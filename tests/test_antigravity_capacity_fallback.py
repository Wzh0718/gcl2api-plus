"""模型级容量 503：降级链、短窗口熔断与 Retry-After 透传。

线上样本（2026-11-28 10:04，beiep-嵌入 / gemini-2.5-flash-lite）：同一请求
两次 503 后直接把上游错误交给客户端；同一秒另一账号却 200。容量窗口是
「按请求槽位拒绝」，不是整段全黑，所以这里只做三件事：

- 窗口内入口直接改投降级链下一个模型（有链才跳，没链照常尝试）；
- 换号/原号都用完仍 503 时，若配置了降级链就换模型重投一次；
- 把上游 RetryInfo 的秒数落成 Retry-After，客户端不会立刻重打。
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import Response

import src.api.antigravity as agy
import src.storage_adapter as storage
from src.antigravity_capacity_window import (
    capacity_window_remaining,
    record_capacity_window,
    reset_capacity_windows,
)

LITE = "gemini-2.5-flash-lite"
FLASH = "gemini-2.5-flash"

# 与线上抓到的一致：模型级容量耗尽 + RetryInfo.retryDelay
CAPACITY_BODY = (
    '{"error": {"code": 503, "message": "No capacity available for model '
    f'{LITE} on the server", "status": "UNAVAILABLE", '
    '"details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", '
    '"reason": "MODEL_CAPACITY_EXHAUSTED", '
    '"domain": "cloudcode-pa.googleapis.com", '
    f'"metadata": {{"error_number": "2010", "model": "{LITE}"}}}}, '
    '{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "38s"}]}}'
)


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


async def _awaitable(value):
    return value


@pytest.fixture(autouse=True)
def _clean_capacity_windows():
    reset_capacity_windows()
    yield
    reset_capacity_windows()


def _patch_common(monkeypatch, chain, max_retries=2):
    monkeypatch.setattr(agy, "get_antigravity_ttft_fallback_seconds", lambda: _awaitable(0))
    monkeypatch.setattr(agy, "get_antigravity_api_url", lambda: _awaitable("http://agy.test"))
    monkeypatch.setattr(agy, "get_antigravity_model_fallback_chain", lambda: _awaitable(chain))
    monkeypatch.setattr(
        agy,
        "get_retry_config",
        lambda: _awaitable(
            {
                "text_codes": (429, 502, 503),
                "retry_enabled": True,
                "max_retries": max_retries,
                "retry_interval": 0.01,
            }
        ),
    )
    monkeypatch.setattr(agy, "get_auto_ban_error_codes", lambda: _awaitable([]))
    monkeypatch.setattr(agy, "check_should_auto_ban", lambda status: _awaitable(False))
    monkeypatch.setattr(agy, "handle_error_with_retry", lambda *a, **k: _awaitable(True))
    monkeypatch.setattr(agy, "record_api_call_error", lambda *a, **k: _awaitable(None))
    monkeypatch.setattr(agy, "record_api_call_success", lambda *a, **k: _awaitable(None))
    monkeypatch.setattr(
        agy, "_alert_all_accounts_unavailable_if_needed", lambda *a, **k: _awaitable(False)
    )

    records = []

    class _Recorder:
        async def record(self, **kwargs):
            records.append(kwargs)

    monkeypatch.setattr(agy, "get_billing_recorder", lambda: _awaitable(_Recorder()))
    monkeypatch.setattr(
        storage,
        "get_storage_adapter",
        lambda: _awaitable(
            SimpleNamespace(get_credential_state=lambda *a, **k: _awaitable({"disabled": False}))
        ),
    )
    return records


def _one_credential(monkeypatch):
    cred = ("cred1.json", {"access_token": "token", "project_id": "proj"})

    async def fake_get_valid_credential(mode=None, model_name=None, **kwargs):
        return cred

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )
    return cred


def _body(model=LITE):
    return {"model": model, "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}}


@pytest.mark.asyncio
async def test_non_stream_capacity_503_falls_back_to_next_model(monkeypatch):
    """换号与原号补试都用完仍 503：换到链上下一个模型，而不是把 503 交回去。"""
    _patch_common(monkeypatch, [LITE, FLASH])
    _one_credential(monkeypatch)

    sent = []

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None, transport=None):
        sent.append(json["model"])
        if json["model"] == LITE:
            return _FakeResponse(503, CAPACITY_BODY.encode())
        return _FakeResponse(200, b'{"response": {"candidates": []}}')

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    resp = await agy.non_stream_request(_body())

    assert resp.status_code == 200
    # 原号 + 原号补试都在 LITE 上，然后才降级
    assert sent == [LITE, LITE, FLASH]


@pytest.mark.asyncio
async def test_capacity_503_on_two_accounts_triggers_chain_fallback(monkeypatch):
    """A 503、换号后 B 也 503：这才判成模型级窗口，第 3 次改投降级模型。

    账号级 503 才值得在同一模型上继续换号；两个不同账号在同一模型上都拿到
    容量 503，换第三个号同样不会成功，直接走降级链。
    """
    records = _patch_common(monkeypatch, [LITE, FLASH])
    creds = {
        "a.json": {"access_token": "token-a", "project_id": "proj", "user_email": "a@gmail.com"},
        "b.json": {"access_token": "token-b", "project_id": "proj", "user_email": "b@gmail.com"},
    }

    async def fake_get_valid_credential(mode=None, model_name=None, exclude_filenames=None, **kwargs):
        for name, data in creds.items():
            if exclude_filenames and name in exclude_filenames:
                continue
            return name, dict(data)
        return None

    monkeypatch.setattr(
        agy.credential_manager, "get_valid_credential", fake_get_valid_credential
    )

    sent = []

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None, transport=None):
        sent.append((json["model"], headers["Authorization"].split()[-1]))
        if json["model"] == LITE:
            return _FakeResponse(503, CAPACITY_BODY.encode())
        return _FakeResponse(200, b'{"response": {"candidates": []}}')

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    resp = await agy.non_stream_request(_body())

    assert resp.status_code == 200
    assert sent == [
        (LITE, "token-a"),   # 原号
        (LITE, "token-b"),   # 换号：同一模型第二个账号
        (FLASH, "token-a"),  # 两个账号都 503 → 改投降级模型，账号重新按轮转选
    ]
    # 监控据此区分「网关改投」与「上游路由」：这次只有一个网关跳
    assert records[-1]["model_fallback"] == {
        "reason": "capacity_503",
        "events": [{"reason": "capacity_503", "from": LITE, "to": FLASH}],
    }


@pytest.mark.asyncio
async def test_non_stream_capacity_503_without_chain_keeps_upstream_error(monkeypatch):
    """没有降级链：维持原行为，但 503 要带 Retry-After，客户端才不会立刻重打。"""
    _patch_common(monkeypatch, [])
    _one_credential(monkeypatch)

    sent = []

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None, transport=None):
        sent.append(json["model"])
        return _FakeResponse(503, CAPACITY_BODY.encode())

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    resp = await agy.non_stream_request(_body())

    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "38"
    assert json.loads(bytes(resp.body))["error"]["details"][1]["retryDelay"] == "38s"
    assert sent == [LITE, LITE]
    # 记了窗口，后续请求入口可以跳
    assert capacity_window_remaining(LITE) > 0


@pytest.mark.asyncio
async def test_non_stream_entry_skips_model_in_capacity_window(monkeypatch):
    """窗口内的模型不再空撞：入口直接改投链上下一个模型。"""
    records = _patch_common(monkeypatch, [LITE, FLASH])
    _one_credential(monkeypatch)
    record_capacity_window(LITE, 38)

    sent = []

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None, transport=None):
        sent.append(json["model"])
        return _FakeResponse(200, b'{"response": {"candidates": []}}')

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    resp = await agy.non_stream_request(_body())

    assert resp.status_code == 200
    assert sent == [FLASH]
    assert records[-1]["model_fallback"] == {
        "reason": "capacity_window",
        "events": [{"reason": "capacity_window", "from": LITE, "to": FLASH}],
    }


@pytest.mark.asyncio
async def test_non_stream_account_level_503_still_swaps_account(monkeypatch):
    """账号级 503（无容量标记）不降级：保持换号语义。"""
    _patch_common(monkeypatch, [LITE, FLASH])
    _one_credential(monkeypatch)

    sent = []

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None, transport=None):
        sent.append(json["model"])
        return _FakeResponse(503, b'{"error": {"message": "The service is currently unavailable"}}')

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    resp = await agy.non_stream_request(_body())

    assert resp.status_code == 503
    # 账号级 503 仍然在同一个模型上按退避重试，不降级、不记容量窗口
    assert set(sent) == {LITE}
    assert "retry-after" not in resp.headers
    assert capacity_window_remaining(LITE) == 0


@pytest.mark.asyncio
async def test_stream_capacity_503_falls_back_to_next_model(monkeypatch):
    records = _patch_common(monkeypatch, [LITE, FLASH])
    _one_credential(monkeypatch)

    sent = []

    async def fake_stream_post_async(url=None, body=None, native=False, headers=None, proxy_url=None, transport=None, **kwargs):
        sent.append(body["model"])
        if body["model"] == LITE:
            yield Response(content=CAPACITY_BODY.encode(), status_code=503)
        else:
            yield 'data: {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}\n\n'

    monkeypatch.setattr(agy, "stream_post_async", fake_stream_post_async)

    chunks = [item async for item in agy.stream_request(_body())]

    assert sent == [LITE, LITE, FLASH]
    assert records[-1]["model_fallback"]["events"] == [
        {"reason": "capacity_503", "from": LITE, "to": FLASH}
    ]
    assert not any(isinstance(item, Response) and item.status_code == 503 for item in chunks)


@pytest.mark.asyncio
async def test_stream_capacity_503_without_chain_keeps_retry_after(monkeypatch):
    _patch_common(monkeypatch, [])
    _one_credential(monkeypatch)

    async def fake_stream_post_async(url=None, body=None, native=False, headers=None, proxy_url=None, transport=None, **kwargs):
        yield Response(content=CAPACITY_BODY.encode(), status_code=503)

    monkeypatch.setattr(agy, "stream_post_async", fake_stream_post_async)

    chunks = [item async for item in agy.stream_request(_body())]

    assert len(chunks) == 1
    assert chunks[0].status_code == 503
    assert chunks[0].headers["retry-after"] == "38"


@pytest.mark.asyncio
async def test_plain_success_records_no_fallback_trace(monkeypatch):
    """没有改投的普通请求不写 model_fallback：监控不该为一跳都没有的请求加字段。"""
    records = _patch_common(monkeypatch, [LITE, FLASH])
    _one_credential(monkeypatch)

    async def fake_post_async(url=None, json=None, headers=None, proxy_url=None, timeout=None, transport=None):
        return _FakeResponse(200, b'{"response": {"candidates": []}}')

    monkeypatch.setattr(agy, "post_async", fake_post_async)

    resp = await agy.non_stream_request(_body())

    assert resp.status_code == 200
    assert "model_fallback" not in records[-1]


def test_trace_ignores_empty_and_identical_hops():
    from src.model_fallback_trace import ModelFallbackTrace

    trace = ModelFallbackTrace()
    trace.record("capacity_503", "", LITE)
    trace.record("capacity_503", LITE, LITE)
    assert trace.snapshot() is None
    trace.record("capacity_503", LITE, FLASH)
    assert trace.snapshot()["events"] == [
        {"reason": "capacity_503", "from": LITE, "to": FLASH}
    ]
