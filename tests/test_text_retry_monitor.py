"""验证文本换号在监控分页、脱离正文采集及异常退出时的表现。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Response
from test_antigravity_model_fallback import _patch_common
from test_call_monitor import _make_adapter, _record_kwargs

from src import antigravity_pacing
from src.api import antigravity
from src.billing import BillingRecorder
from src.call_monitor import CallMonitor
from src.panel import call_monitor as panel
from src.request_capture import capture_managed


@pytest.fixture
async def monitor_adapter(tmp_path, monkeypatch):
    """为监控与计费共享隔离的测试数据库。"""
    adapter = await _make_adapter(tmp_path, monkeypatch)
    try:
        yield adapter
    finally:
        await adapter.close()


async def test_retry_monitor_pages_without_capture(monitor_adapter, monkeypatch):
    """并发写入的摘要按行隔离，最近及历史分页复用同一批账号资料。"""
    adapter = monitor_adapter
    monitor = CallMonitor(adapter)
    recorder = BillingRecorder(adapter._backend)
    monkeypatch.setattr(
        "src.call_monitor.get_call_monitor", AsyncMock(return_value=monitor)
    )
    monkeypatch.setattr(panel, "get_call_monitor", AsyncMock(return_value=monitor))
    monkeypatch.setattr(panel, "get_storage_adapter", AsyncMock(return_value=adapter))
    lookup = AsyncMock(return_value={"A": {"user_email": "first@example.test"}})
    monkeypatch.setattr(adapter._backend, "get_call_monitor_account_metadata", lookup)
    token = capture_managed.set(True)
    try:
        await asyncio.gather(
            *(
                recorder.record(
                    request_id=name,
                    credential_name=name,
                    model="gemini-2.5-flash",
                    success=True,
                    status_code=200,
                    usage_metadata={"promptTokenCount": 10, "candidatesTokenCount": 5},
                    text_account_retry={
                        "state": "sent",
                        "attempts": [
                            {"credential_name": "A", "status_code": 429},
                            {"credential_name": name, "status_code": 200},
                        ],
                    },
                )
                for name in ("B", "C", "B")
            )
        )
        await monitor.record(**_record_kwargs("old"))
        summary = await adapter._backend.get_billing_summary("today")
        assert (summary["total_tokens"], summary["success_count"]) == (30, 2)
    finally:
        capture_managed.reset(token)
    for window in ("recent", "today"):
        lookup.reset_mock()
        page = await panel.call_monitor_realtime(range=window)
        assert (page["total"], page["counters"]["failed"]) == (3, 0)
        lookup.assert_awaited_once()
        for row in page["records"]:
            features = row["request_features"]
            if row["request_id"] == "old":
                assert "text_account_retry" not in features
                continue
            assert "capture_id" not in features
            first, second = features["text_account_retry"]["attempts"]
            assert first["user_email"] == "first@example.test"
            assert second["credential_name"] == row["request_id"]
            detail = await panel.call_monitor_content(record_id=row["id"])
            attempts = json.loads(detail.body)["request_features"][
                "text_account_retry"
            ]["attempts"]
            assert [attempt["credential_name"] for attempt in attempts] == [
                "A",
                row["request_id"],
            ]
        assert (await panel.call_monitor_realtime(range=window, failed_only=True))[
            "total"
        ] == 0


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("first_status", [429, 502, 504])
@pytest.mark.parametrize(
    "alternate",
    ["cooldown", "slot_busy", "usage_limit", "connect_error", "ttft_timeout"],
)
async def test_alternate_without_response(
    monitor_adapter, monkeypatch, streaming, first_status, alternate
):
    """区分备选被本地拒绝与备选已发送但没有 HTTP 响应。"""
    model = "gemini-2.5-flash"
    chain = [model, "gemini-3.1-flash-lite"] if alternate == "ttft_timeout" else []
    _patch_common(monkeypatch, chain, ttft_seconds=0.025 if chain else 0)
    monkeypatch.setattr(
        antigravity, "get_antigravity_stream2nostream", AsyncMock(return_value=False)
    )
    settings = {
        "retry_enabled": True,
        "text_codes": [first_status],
        "max_retries": 1,
        "retry_interval": 0,
    }
    monkeypatch.setattr(
        antigravity, "get_retry_config", AsyncMock(return_value=settings)
    )
    creds = [(name, {"access_token": name, "project_id": name}) for name in ("A", "B")]
    monkeypatch.setattr(
        antigravity.credential_manager,
        "get_valid_credential",
        AsyncMock(side_effect=creds),
    )
    monkeypatch.setattr(
        antigravity,
        "_text_credential_is_available",
        AsyncMock(side_effect=lambda name, _: name == "A" or alternate != "cooldown"),
    )
    if alternate == "slot_busy":
        acquire = antigravity_pacing.acquire_paced_credential_slot

        async def acquire_slot(name, **kwargs):
            if name == "B":
                raise antigravity_pacing.CredentialSlotWaitTimeout(name)
            return await acquire(name, **kwargs)

        monkeypatch.setattr(
            antigravity_pacing, "acquire_paced_credential_slot", acquire_slot
        )
    if alternate == "usage_limit":
        monkeypatch.setattr(
            antigravity,
            "begin_account_usage",
            AsyncMock(side_effect=[None, antigravity.AccountUsageLimitReached(30)]),
        )
    storage = monitor_adapter._backend
    recorder = SimpleNamespace(record=AsyncMock(wraps=BillingRecorder(storage).record))
    monkeypatch.setattr(
        "src.call_monitor.get_call_monitor",
        AsyncMock(return_value=CallMonitor(monitor_adapter)),
    )
    monkeypatch.setattr(
        antigravity, "get_billing_recorder", AsyncMock(return_value=recorder)
    )
    sent = []

    async def post(**kwargs):
        name = kwargs["headers"]["Authorization"].split()[-1]
        sent.append((name, kwargs.get("body", kwargs.get("json"))))
        if name == "B" and alternate == "ttft_timeout":
            kwargs["first_output_deadline"].start()
            await asyncio.Event().wait()
        if name == "B":
            raise httpx.ConnectError("no response")
        return httpx.Response(
            first_status, json={"error": "from A"}, headers={"Retry-After": "30"}
        )

    async def stream(**kwargs):
        upstream = await post(**kwargs)
        yield Response(upstream.content, upstream.status_code, headers=upstream.headers)

    monkeypatch.setattr(antigravity, "post_async", post)
    monkeypatch.setattr(antigravity, "stream_post_async", stream)
    body = {"model": model, "request": {"contents": []}}
    if streaming:
        chunks = antigravity.stream_request(body)
        try:
            result = await anext(chunks)
            recorder.record.assert_awaited_once()
        finally:
            await chunks.aclose()
    else:
        result = await antigravity.non_stream_request(body)
    final_status = {"connect_error": 500, "ttft_timeout": 504}.get(alternate)
    can_send = final_status is not None
    assert result.status_code == (final_status if can_send else first_status)
    if not can_send:
        assert json.loads(result.body) == {"error": "from A"}
        assert result.headers["Retry-After"] == "30"
    assert [name for name, _ in sent] == (["A", "B"] if can_send else ["A"])
    recorder.record.assert_awaited_once()
    billed = recorder.record.await_args.kwargs
    account, payload = sent[-1]
    assert billed["credential_name"] == account
    assert billed["request_id"] == payload["requestId"]
    assert billed["upstream_request"] == payload
    assert billed["model"] == model
    assert billed["success"] is False
    assert billed["status_code"] == result.status_code
    if not can_send and first_status == 429:
        assert billed["rate_limit"].source == "upstream"
    else:
        assert "rate_limit" not in billed
    summary = billed["text_account_retry"]
    assert summary["state"] == ("sent" if can_send else "not_sent")
    first, second = summary["attempts"]
    assert (first["credential_name"], first["status_code"]) == ("A", first_status)
    assert second["credential_name"] == "B"
    assert "status_code" not in second
    assert ("elapsed_ms" in second) == can_send
    (daily,) = await storage.list_billing_daily()
    assert (daily["credential_name"], daily["failed_count"]) == (account, 1)
    (row,) = await storage.list_call_records()
    assert row["credential_name"] == account
    assert row["request_id"] == payload["requestId"]
    assert row["status_code"] == result.status_code
    assert row["request_features"]["text_account_retry"] == summary
