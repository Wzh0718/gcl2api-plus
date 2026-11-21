"""并发闸门的路径级测试（2026-10-22）。

覆盖要点：槽位在流式/非流式/中途放弃三条路径上都必须归还——一旦泄漏，
该账号的并发上限会被永久吃掉，表现为「请求越来越慢直到全部卡死」。
"""

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import Response

import config
from src import antigravity_pacing as pacing
from src.antigravity_usage import CONFIG_KEY
from src.api import antigravity as agy


@pytest.fixture(autouse=True)
def _enable_pacing(monkeypatch):
    # conftest 默认关闭 pacing，这里显式打开
    monkeypatch.setenv("ANTIGRAVITY_PACING_ENABLED", "true")
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "15")
    monkeypatch.setenv("ANTIGRAVITY_PACING_BASE_MS", "0")
    monkeypatch.setenv("ANTIGRAVITY_PACING_JITTER_PCT", "0")
    monkeypatch.setenv("ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS", "0.05")
    pacing.reset_state()
    yield
    pacing.reset_state()


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.body = content
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="ignore")

    def json(self):
        return json.loads(self.text)


async def _awaitable(value):
    return value


def _patch_common(monkeypatch, cred=("cred1.json", {"access_token": "t", "project_id": "p"})):
    monkeypatch.setattr(agy, "get_antigravity_api_url", lambda: _awaitable("http://agy.test"))
    monkeypatch.setattr(agy, "get_antigravity_model_fallback_chain", lambda: _awaitable([]))
    monkeypatch.setattr(
        agy,
        "get_retry_config",
        lambda: _awaitable(
            {"retry_enabled": False, "max_retries": 0, "retry_interval": 0.01}
        ),
    )
    monkeypatch.setattr(agy, "get_auto_ban_error_codes", lambda: _awaitable([]))
    monkeypatch.setattr(agy, "handle_error_with_retry", lambda *a, **k: _awaitable(False))

    async def _no_err(*a, **k):
        return None

    monkeypatch.setattr(agy, "record_api_call_error", _no_err)
    monkeypatch.setattr(agy, "record_api_call_success", _no_err)
    monkeypatch.setattr(
        agy, "_alert_all_accounts_unavailable_if_needed", lambda *a, **k: _awaitable(False)
    )

    class _Recorder:
        async def record(self, **kwargs):
            return None

    monkeypatch.setattr(agy, "get_billing_recorder", lambda: _awaitable(_Recorder()))

    async def fake_get_valid_credential(mode=None, model_name=None, **kwargs):
        return cred

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )
    return cred


def _body(model: str = "gemini-2.5-flash"):
    return {
        "model": model,
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }


def _inflight(name: str) -> int:
    slot = pacing._SLOTS.get(name)
    return slot.inflight if slot is not None else 0


@pytest.mark.asyncio
async def test_stream_releases_slot_after_completion(monkeypatch):
    cred = _patch_common(monkeypatch)

    async def fake_stream(**kwargs):
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)

    chunks = [item async for item in agy.stream_request(_body())]
    assert chunks
    assert _inflight(cred[0]) == 0


@pytest.mark.asyncio
async def test_stream_releases_slot_when_consumer_abandons(monkeypatch):
    """客户端中途断开（提前 aclose）：槽位必须归还。"""
    cred = _patch_common(monkeypatch)

    release = AsyncMock()
    monkeypatch.setattr(agy, "release_dynamic_warmup_slot", release)

    async def fake_stream(**kwargs):
        yield 'data: {"candidates": [{"content": {"parts": [{"text": "a"}]}}]}\n\n'
        await asyncio.sleep(10)  # 上游还有数据，但消费者已经走了
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)

    stream = agy.stream_request(_body())
    first = await stream.__anext__()
    assert first
    assert _inflight(cred[0]) == 0  # 文本流无需单账号槽位

    await stream.aclose()
    release.assert_awaited_once_with(cred[0])
    assert _inflight(cred[0]) == 0


@pytest.mark.asyncio
async def test_non_stream_releases_slot(monkeypatch):
    cred = _patch_common(monkeypatch)

    async def fake_post(**kwargs):
        return _FakeResponse(200, json.dumps({"candidates": []}).encode())

    monkeypatch.setattr(agy, "post_async", fake_post)

    response = await agy.non_stream_request(_body())
    assert response.status_code == 200
    assert _inflight(cred[0]) == 0


@pytest.mark.asyncio
async def test_account_usage_shared_across_keys_and_streaming(monkeypatch, tmp_path):
    from src import storage_adapter

    cred = _patch_common(monkeypatch)
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("WORKERS", "1")
    storage = storage_adapter.StorageAdapter()
    await storage.initialize()
    assert await storage.store_credential(*cred, mode="antigravity")
    monkeypatch.setattr(storage_adapter, "_storage_adapter", storage)
    monkeypatch.setattr(storage, "get_api_key_record", lambda key: _awaitable({}))
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {
        CONFIG_KEY: {"enabled": True, "generated_tokens_per_minute": 10},
    })
    sent = []

    async def fake_stream(**kwargs):
        sent.append("stream")
        yield b'data: {"usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":7,"thoughtsTokenCount":3}}'

    async def fake_post(**kwargs):
        sent.append("post")
        return _FakeResponse(200, b'{"candidates":[]}')

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)
    monkeypatch.setattr(agy, "post_async", fake_post)

    stream = agy.stream_request(_body(), native=True, api_key_id="first-key")
    assert await anext(stream)
    await stream.aclose()

    response = await agy.non_stream_request(_body("gemini-2.5-flash-lite"), api_key_id="second-key")
    assert response.status_code == 429
    assert response.rate_limit.as_dict() == {
        "source": "gateway",
        "reason": "account_usage_limit",
        "rules": ["generated_tokens_per_minute"],
    }
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert sent == ["stream"]
    assert _inflight(cred[0]) == 0


@pytest.mark.asyncio
async def test_concurrent_streams_with_limit_one_all_release(monkeypatch):
    """并发上限=1 时，两个并发流式请求各自归还槽位，不会泄漏或死锁。"""
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "1")
    cred = _patch_common(monkeypatch)

    async def fake_stream(**kwargs):
        await asyncio.sleep(0.02)
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)

    async def _consume():
        return [item async for item in agy.stream_request(_body())]

    results = await asyncio.gather(_consume(), _consume())
    assert all(results)
    assert _inflight(cred[0]) == 0


@pytest.mark.asyncio
async def test_disabled_pacing_never_registers_slot(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PACING_ENABLED", "false")
    cred = _patch_common(monkeypatch)

    async def fake_stream(**kwargs):
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)

    _ = [item async for item in agy.stream_request(_body())]
    assert cred[0] not in pacing._SLOTS


@pytest.mark.asyncio
async def test_stream_retry_rebuilds_envelope_for_new_credential(monkeypatch):
    account_a = ("a.json", {"access_token": "token-a", "project_id": "project-a"})
    account_b = ("b.json", {"access_token": "token-b", "project_id": "project-b"})
    _patch_common(monkeypatch, account_a)
    monkeypatch.setattr(
        agy,
        "get_retry_config",
        lambda: _awaitable({"retry_enabled": True, "max_retries": 1, "retry_interval": 0}),
    )
    monkeypatch.setattr(agy, "_text_credential_is_available", lambda *a: _awaitable(True))
    monkeypatch.setattr(agy, "handle_error_with_retry", lambda *a, **k: _awaitable(True))
    credentials = iter((account_a, account_b))

    async def select_credential(**kwargs):
        return next(credentials)

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", select_credential)
    agy._session_states.clear()
    upstream_bodies = []

    async def fake_stream(**kwargs):
        upstream_bodies.append(copy.deepcopy(kwargs["body"]))
        if len(upstream_bodies) == 1:
            yield Response(
                content=b'{"error":{"reason":"UNAVAILABLE"}}',
                status_code=503,
                media_type="application/json",
            )
        else:
            yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)
    chunks = [item async for item in agy.stream_request(_body())]

    assert len(upstream_bodies) == 2
    first, second = upstream_bodies
    assert second["request"]["sessionId"] != first["request"]["sessionId"]
    assert second["requestId"] != first["requestId"]
    for key in ("cascade_id", "root_cascade_id", "request_id", "trajectory_id"):
        assert second["request"]["labels"][key] != first["request"]["labels"][key]
    assert not any(isinstance(item, Response) and item.status_code == 503 for item in chunks)


@pytest.mark.asyncio
async def test_stream_uses_rotated_account_even_when_local_slot_is_full(monkeypatch):
    account_a = ("a.json", {"access_token": "token-a", "project_id": "project-a"})
    account_b = ("b.json", {"access_token": "token-b", "project_id": "project-b"})
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "1")
    _patch_common(monkeypatch, account_a)
    monkeypatch.setattr(
        agy,
        "get_retry_config",
        lambda: _awaitable({"retry_enabled": True, "max_retries": 1, "retry_interval": 0}),
    )
    monkeypatch.setattr(agy, "_text_credential_is_available", lambda *a: _awaitable(True))
    credentials = iter((account_a, account_b))

    async def select_credential(**kwargs):
        return next(credentials, None)

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", select_credential)
    await pacing.acquire_limited_credential_slot(account_a[0])
    upstream_tokens = []

    async def fake_stream(**kwargs):
        upstream_tokens.append(kwargs["headers"]["Authorization"])
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)
    try:
        chunks = [item async for item in agy.stream_request(_body())]
    finally:
        pacing.release_limited_credential_slot(account_a[0])

    assert upstream_tokens == ["Bearer token-a"]
    assert not any(isinstance(item, Response) and item.status_code == 429 for item in chunks)
    assert _inflight(account_b[0]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
async def test_text_keeps_admission_and_skips_start_pacing(monkeypatch, streaming):
    cred = _patch_common(monkeypatch)
    events = []

    async def acquire(name):
        events.append("acquire")
        return 0.0

    async def pace(name):
        events.append("pace")
        return 0.0

    monkeypatch.setattr(pacing, "acquire_credential_slot", acquire)
    monkeypatch.setattr(pacing, "pace_credential", pace)

    async def fake_stream(**kwargs):
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)
    if streaming:
        assert [item async for item in agy.stream_request(_body())]
    else:
        monkeypatch.setattr(agy, "post_async", AsyncMock(return_value=_FakeResponse(200, b'{"candidates":[]}')))
        assert (await agy.non_stream_request(_body())).status_code == 200

    assert events == ["acquire"]
    assert _inflight(cred[0]) == 0


@pytest.mark.asyncio
async def test_non_stream_retry_rebuilds_envelope_for_new_credential(monkeypatch):
    account_a = ("a.json", {"access_token": "token-a", "project_id": "project-a"})
    account_b = ("b.json", {"access_token": "token-b", "project_id": "project-b"})
    _patch_common(monkeypatch, account_a)
    monkeypatch.setattr(
        agy,
        "get_retry_config",
        lambda: _awaitable({"retry_enabled": True, "max_retries": 1, "retry_interval": 0}),
    )
    monkeypatch.setattr(agy, "_text_credential_is_available", lambda *a: _awaitable(True))
    monkeypatch.setattr(agy, "handle_error_with_retry", lambda *a, **k: _awaitable(True))
    credentials = iter((account_a, account_b))

    async def select_credential(**kwargs):
        return next(credentials)

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", select_credential)
    agy._session_states.clear()
    upstream_bodies = []

    async def fake_post(**kwargs):
        upstream_bodies.append(copy.deepcopy(kwargs["json"]))
        if len(upstream_bodies) == 1:
            return _FakeResponse(503, b'{"error":{"reason":"UNAVAILABLE"}}')
        return _FakeResponse(200, b'{"candidates":[]}')

    monkeypatch.setattr(agy, "post_async", fake_post)
    response = await agy.non_stream_request(_body())

    assert response.status_code == 200
    assert len(upstream_bodies) == 2
    first, second = upstream_bodies
    assert second["request"]["sessionId"] != first["request"]["sessionId"]
    assert second["requestId"] != first["requestId"]
    for key in ("cascade_id", "root_cascade_id", "request_id", "trajectory_id"):
        assert second["request"]["labels"][key] != first["request"]["labels"][key]


@pytest.mark.asyncio
async def test_non_stream_uses_rotated_account_even_when_local_slot_is_full(monkeypatch):
    account_a = ("a.json", {"access_token": "token-a", "project_id": "project-a"})
    account_b = ("b.json", {"access_token": "token-b", "project_id": "project-b"})
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "1")
    _patch_common(monkeypatch, account_a)
    monkeypatch.setattr(
        agy,
        "get_retry_config",
        lambda: _awaitable({"retry_enabled": True, "max_retries": 1, "retry_interval": 0}),
    )
    monkeypatch.setattr(agy, "_text_credential_is_available", lambda *a: _awaitable(True))
    credentials = iter((account_a, account_b))

    async def select_credential(**kwargs):
        return next(credentials, None)

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", select_credential)
    await pacing.acquire_limited_credential_slot(account_a[0])
    upstream_tokens = []

    async def fake_post(**kwargs):
        upstream_tokens.append(kwargs["headers"]["Authorization"])
        return _FakeResponse(200, b'{"candidates":[]}')

    monkeypatch.setattr(agy, "post_async", fake_post)
    try:
        response = await agy.non_stream_request(_body())
    finally:
        pacing.release_limited_credential_slot(account_a[0])

    assert response.status_code == 200
    assert upstream_tokens == ["Bearer token-a"]
    assert _inflight(account_b[0]) == 0


@pytest.mark.asyncio
async def test_pool_uses_all_accounts_even_with_occupied_slot(monkeypatch):
    accounts = [
        (f"account-{index:02d}.json", {
            "access_token": f"token-{index:02d}",
            "project_id": f"project-{index:02d}",
        })
        for index in range(15)
    ]
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "1")
    _patch_common(monkeypatch, accounts[0])
    monkeypatch.setattr(
        agy,
        "get_retry_config",
        lambda: _awaitable({"retry_enabled": True, "max_retries": 15, "retry_interval": 0}),
    )
    monkeypatch.setattr(agy, "_text_credential_is_available", lambda *a: _awaitable(True))
    call_counts = {name: 0 for name, _ in accounts}

    async def select_credential(**kwargs):
        excluded = kwargs.get("exclude_filenames") or set()
        candidates = [item for item in accounts if item[0] not in excluded]
        if not candidates:
            return None
        selected = min(candidates, key=lambda item: (call_counts[item[0]], item[0]))
        call_counts[selected[0]] += 1
        return selected

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", select_credential)
    await pacing.acquire_limited_credential_slot(accounts[0][0])
    upstream_tokens = []

    async def fake_stream(**kwargs):
        upstream_tokens.append(kwargs["headers"]["Authorization"])
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)

    async def consume():
        return [item async for item in agy.stream_request(_body())]

    try:
        results = await asyncio.gather(*(consume() for _ in range(30)))
    finally:
        pacing.release_limited_credential_slot(accounts[0][0])

    local_429s = [
        item
        for chunks in results
        for item in chunks
        if isinstance(item, Response) and item.status_code == 429
    ]
    assert local_429s == []
    assert len(upstream_tokens) == 30
    assert set(upstream_tokens) == {
        f"Bearer token-{index:02d}" for index in range(15)
    }
    assert all(_inflight(name) == 0 for name, _ in accounts)


@pytest.mark.asyncio
async def test_fifteen_healthy_accounts_split_thirty_requests_evenly(monkeypatch):
    from collections import Counter

    accounts = [
        (f"account-{index:02d}.json", {
            "access_token": f"token-{index:02d}",
            "project_id": f"project-{index:02d}",
        })
        for index in range(15)
    ]
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "2")
    _patch_common(monkeypatch, accounts[0])
    monkeypatch.setattr(agy, "_text_credential_is_available", lambda *a: _awaitable(True))
    call_counts = {name: 0 for name, _ in accounts}

    async def select_credential(**kwargs):
        excluded = kwargs.get("exclude_filenames") or set()
        candidates = [item for item in accounts if item[0] not in excluded]
        selected = min(candidates, key=lambda item: (call_counts[item[0]], item[0]))
        call_counts[selected[0]] += 1
        return selected

    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", select_credential)
    upstream_tokens = []

    async def fake_stream(**kwargs):
        upstream_tokens.append(kwargs["headers"]["Authorization"])
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)

    async def consume():
        return [item async for item in agy.stream_request(_body())]

    results = await asyncio.gather(*(consume() for _ in range(30)))

    assert not any(
        isinstance(item, Response) and item.status_code == 429
        for chunks in results
        for item in chunks
    )
    assert Counter(upstream_tokens) == {
        f"Bearer token-{index:02d}": 2 for index in range(15)
    }
    assert all(_inflight(name) == 0 for name, _ in accounts)


@pytest.mark.asyncio
async def test_stream_unknown_model_returns_400_without_upstream(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        agy, "_get_available_models_for_credential", lambda **kwargs: _awaitable(set())
    )

    async def unexpected_stream(**kwargs):
        raise AssertionError("未知模型不得触达上游")
        yield

    monkeypatch.setattr(agy, "stream_post_async", unexpected_stream)
    chunks = [
        item async for item in agy.stream_request(_body("gemini-stream-unknown-model"))
    ]

    assert len(chunks) == 1
    assert chunks[0].status_code == 400
    assert json.loads(chunks[0].body)["error"]["status"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_non_stream_unknown_model_returns_400_without_upstream(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        agy, "_get_available_models_for_credential", lambda **kwargs: _awaitable(set())
    )

    async def unexpected_post(**kwargs):
        raise AssertionError("未知模型不得触达上游")

    monkeypatch.setattr(agy, "post_async", unexpected_post)
    response = await agy.non_stream_request(_body("gemini-non-stream-unknown-model"))

    assert response.status_code == 400
    assert json.loads(response.body)["error"]["status"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_preselected_limited_account_switches_without_upstream_retry(monkeypatch):
    from src.antigravity_usage import AccountUsageLimitReached

    first = _patch_common(monkeypatch)
    second = ("other.json", {"access_token": "other-token", "project_id": "other-project"})
    select = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(agy.credential_manager, "get_valid_credential", select)
    gate = AsyncMock(side_effect=[AccountUsageLimitReached(15), None])
    monkeypatch.setattr(agy, "begin_account_usage", gate)
    post = AsyncMock(return_value=_FakeResponse(200, b'{"candidates":[]}'))
    monkeypatch.setattr(agy, "post_async", post)
    response = await agy.non_stream_request(_body())
    assert response.status_code == 200
    assert select.await_args.kwargs["exclude_filenames"] == {first[0]}
    assert [call.args[0] for call in gate.await_args_list] == [first[0], second[0]]
    post.assert_awaited_once()
    assert post.await_args.kwargs["headers"]["Authorization"] == "Bearer other-token"
    assert _inflight(first[0]) == _inflight(second[0]) == 0
