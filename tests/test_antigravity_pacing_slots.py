"""并发闸门的路径级测试（2026-10-22）。

覆盖要点：槽位在流式/非流式/中途放弃三条路径上都必须归还——一旦泄漏，
该账号的并发上限会被永久吃掉，表现为「请求越来越慢直到全部卡死」。
"""

import asyncio
import json

import pytest
from fastapi import Response

from src import antigravity_pacing as pacing
from src.api import antigravity as agy


@pytest.fixture(autouse=True)
def _enable_pacing(monkeypatch):
    # conftest 默认关闭 pacing，这里显式打开
    monkeypatch.setenv("ANTIGRAVITY_PACING_ENABLED", "true")
    monkeypatch.setenv("ANTIGRAVITY_PACING_CONCURRENCY", "15")
    monkeypatch.setenv("ANTIGRAVITY_PACING_BASE_MS", "0")
    monkeypatch.setenv("ANTIGRAVITY_PACING_JITTER_PCT", "0")
    monkeypatch.setattr(pacing, "_SLOT_WAIT_CAP_SECONDS", 0.05)
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

    async def fake_stream(**kwargs):
        yield 'data: {"candidates": [{"content": {"parts": [{"text": "a"}]}}]}\n\n'
        await asyncio.sleep(10)  # 上游还有数据，但消费者已经走了
        yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream)

    stream = agy.stream_request(_body())
    first = await stream.__anext__()
    assert first
    assert _inflight(cred[0]) == 1  # 在飞期间占用一个槽位

    await stream.aclose()
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
