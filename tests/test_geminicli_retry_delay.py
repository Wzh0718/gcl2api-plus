from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Response

from src.api import geminicli, utils


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("status", [400, 429, 500, 503])
@pytest.mark.parametrize("enabled,budget", [(True, 2), (True, 0), (False, 2)])
async def test_geminicli_retry_obeys_single_interval(monkeypatch, streaming, status, enabled, budget):
    manager = SimpleNamespace(
        get_valid_credential=AsyncMock(return_value=(
            "a.json", {"access_token": "a", "project_id": "proj"},
        )),
        set_cred_disabled=AsyncMock(),
    )
    monkeypatch.setattr(geminicli, "credential_manager", manager)
    monkeypatch.setattr(geminicli, "get_code_assist_endpoint", AsyncMock(return_value="https://example.invalid"))
    monkeypatch.setattr(geminicli, "get_auto_ban_error_codes", AsyncMock(return_value=[400]))
    monkeypatch.setattr(utils, "check_should_auto_ban", AsyncMock(return_value=status == 400))
    monkeypatch.setattr(geminicli, "get_retry_config", AsyncMock(return_value={
        "retry_enabled": enabled, "max_retries": budget, "retry_interval": 5,
    }))
    monkeypatch.setattr(geminicli, "record_api_call_error", AsyncMock())
    monkeypatch.setattr(geminicli, "record_api_call_success", AsyncMock())
    waits, sent = [], []
    monkeypatch.setattr(geminicli.asyncio, "sleep", AsyncMock(side_effect=waits.append))

    async def post(**kwargs):
        sent.append(kwargs)
        return httpx.Response(status if len(sent) == 1 else 200, json={"result": "test"})

    async def stream(**kwargs):
        response = await post(**kwargs)
        if response.status_code != 200:
            yield Response(response.content, response.status_code)
        else:
            yield 'data: {"candidates": []}\n\n'

    monkeypatch.setattr(geminicli, "post_async", post)
    monkeypatch.setattr(geminicli, "stream_post_async", stream)
    body = {"model": "gemini-test", "request": {"contents": []}}
    if streaming:
        chunks = [chunk async for chunk in geminicli.stream_request(body)]
        result = chunks[-1]
        final_status = result.status_code if isinstance(result, Response) else 200
    else:
        final_status = (await geminicli.non_stream_request(body)).status_code

    retries = int(enabled and budget > 0)
    assert final_status == (200 if retries else status)
    assert len(sent) == 1 + retries
    assert [delay for delay in waits if delay > 0] == [5] * retries
    assert manager.set_cred_disabled.await_count == int(status == 400)
