"""验证文本换号在真实通道入口的发送次数、错误透传和配置热更新。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Response
from test_antigravity_model_fallback import _patch_common

import config
from src import storage_adapter
from src.api import antigravity, geminicli, text_retry, utils
from src.panel import config_routes


@pytest.mark.parametrize("channel", [antigravity, geminicli])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "codes,statuses,names,enabled,expected",
    [
        ([429, 502], [429, 200], "abc", True, ["a", "b"]),
        ([429, 502], [429, 502], "abc", True, ["a", "b"]),
        ([429, 502], [502, 200], "abc", True, ["a", "b"]),
        ([429, 502], [502, 429], "abc", True, ["a", "b"]),
        ([429, 502], [503, 429], "abc", True, ["a", "a"]),
        ([429, 502, 503], [503, 200], "abc", True, ["a", "b"]),
        ([429, 502], [429, 503], "abc", True, ["a", "b"]),
        ([429, 502], [502], "a", True, ["a"]),
        ([429, 502], [502], "lookup_error", True, ["a"]),
        ([429, 502], [429], "abc", False, ["a"]),
        ([], [429], "abc", True, ["a"]),
        ([504], [504, 200], "abc", True, ["a", "b"]),
    ],
)
async def test_request(
    channel,
    streaming,
    codes,
    statuses,
    names,
    enabled,
    expected,
    monkeypatch,
):
    """实际发送的账号、最终响应和计费须符合单次换号约束。"""
    _patch_common(monkeypatch, [])
    monkeypatch.setattr(
        antigravity, "get_antigravity_stream2nostream", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        geminicli, "get_code_assist_endpoint", AsyncMock(return_value="http://test")
    )
    monkeypatch.setattr(channel, "get_auto_ban_error_codes", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        channel,
        "get_retry_config",
        AsyncMock(
            return_value={
                "text_codes": codes,
                "retry_enabled": enabled,
                "max_retries": 5,
                "retry_interval": 0,
            }
        ),
    )
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr(
        antigravity,
        "get_billing_recorder",
        AsyncMock(return_value=recorder),
    )
    monkeypatch.setattr(channel, "record_api_call_error", AsyncMock())
    monkeypatch.setattr(channel, "record_api_call_success", AsyncMock())
    monkeypatch.setattr(utils, "check_should_auto_ban", AsyncMock(return_value=False))
    sent = []

    async def choose(**kwargs):
        if names == "lookup_error" and kwargs.get("exclude_filenames"):
            raise OSError("test selection failure")
        for name in "a" if names == "lookup_error" else names:
            if name not in kwargs.get("exclude_filenames", set()):
                return name, {"access_token": name, "project_id": "project-" + name}
        return None

    def response(kwargs):
        name = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        sent.append(name)
        payload = kwargs.get("json", kwargs.get("body"))
        assert payload["project"] == "project-" + name
        status = statuses[len(sent) - 1]
        return httpx.Response(
            status,
            json={"account": name},
            headers={"Retry-After": "17"},
        )

    async def post(**kwargs):
        return response(kwargs)

    async def stream(**kwargs):
        result = response(kwargs)
        if result.status_code == 200:
            yield 'data: {"candidates": []}\n\n'
        else:
            yield Response(
                result.content,
                result.status_code,
                headers={"Retry-After": "17"},
            )

    monkeypatch.setattr(channel.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(channel, "post_async", post)
    monkeypatch.setattr(channel, "stream_post_async", stream)
    body = {
        "model": "gemini-2.5-flash",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }
    if streaming:
        chunks = [item async for item in channel.stream_request(body)]
        last = chunks[-1]
        result = last if isinstance(last, Response) else Response(status_code=200)
    else:
        result = await channel.non_stream_request(body)
    assert sent == expected
    assert [call.args[1] for call in channel.record_api_call_error.await_args_list] == [
        name for name, status in zip(expected, statuses, strict=True) if status != 200
    ]
    assert result.status_code == statuses[-1]
    if result.status_code != 200:
        assert result.headers["Retry-After"] == "17"
        assert expected[-1].encode() in result.body
    if channel is antigravity:
        recorder.record.assert_awaited_once()
        assert recorder.record.await_args.kwargs["credential_name"] == expected[-1]
        trace = recorder.record.await_args.kwargs.get("text_account_retry")
        if not enabled or statuses[0] not in codes:
            assert trace is None
        else:
            attempts = trace["attempts"]
            assert [item["credential_name"] for item in attempts] == expected
            assert [item["status_code"] for item in attempts] == statuses
            assert all(item["elapsed_ms"] >= 0 and item["model"] for item in attempts)
            assert attempts[0]["retry_after"] == "17"
            if statuses[0] == 429:
                assert attempts[0]["rate_limit"]["source"] == "upstream"
            assert trace["state"] == (
                "sent"
                if len(expected) == 2
                else "selection_failed"
                if names == "lookup_error"
                else "no_candidate"
            )


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "codes,names,statuses,accounts,state",
    [
        ([503], "abc", [503, 200], ["a", "b"], "sent"),
        ([503], "abc", [503, 503, 200], ["a", "b", "b"], "sent"),
        ([503], "abc", [503, 503, 503], ["a", "b", "b"], "sent"),
        ([503], "abc", [503, 502], ["a", "b"], "sent"),
        ([503], "a", [503, 503], ["a", "a"], "no_candidate"),
        ([], "abc", [503, 503], ["a", "a"], None),
        ([429, 503], "abc", [429, 503], ["a", "b"], "sent"),
    ],
)
async def test_antigravity_capacity_retry_order(
    monkeypatch, streaming, codes, names, statuses, accounts, state
):
    """先消耗换号机会；容量型 503 才能在当前账号上额外补试一次。"""
    _patch_common(monkeypatch, [])
    monkeypatch.setattr(
        antigravity, "get_antigravity_stream2nostream", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        antigravity,
        "get_retry_config",
        AsyncMock(
            return_value={
                "text_codes": codes,
                "retry_enabled": True,
                "max_retries": 1,
                "retry_interval": 0,
            }
        ),
    )
    recorder = AsyncMock()
    errors = AsyncMock()
    monkeypatch.setattr(
        antigravity, "get_billing_recorder", AsyncMock(return_value=recorder)
    )
    monkeypatch.setattr(antigravity, "record_api_call_error", errors)

    async def choose(**kwargs):
        for name in names:
            if name not in kwargs.get("exclude_filenames", set()):
                return name, {"access_token": name, "project_id": name}
        return None

    sent = []

    async def post(**kwargs):
        sent.append(kwargs["headers"]["Authorization"].removeprefix("Bearer "))
        status = statuses[len(sent) - 1]
        error = {"code": status, "message": "No capacity available for model"}
        if status == 503:
            error["status"] = "UNAVAILABLE"
            error["details"] = [{"reason": "MODEL_CAPACITY_EXHAUSTED"}]
        return httpx.Response(
            status, json={"error": error}, headers={"Retry-After": "17"}
        )

    async def stream(**kwargs):
        result = await post(**kwargs)
        if result.status_code == 200:
            yield 'data: {"candidates": []}\n\n'
        else:
            yield Response(result.content, result.status_code, headers=result.headers)

    monkeypatch.setattr(antigravity.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(antigravity, "post_async", post)
    monkeypatch.setattr(antigravity, "stream_post_async", stream)
    body = {"model": "gemini-2.5-flash", "request": {"contents": []}}
    if streaming:
        chunks = [item async for item in antigravity.stream_request(body)]
        response = chunks[-1] if isinstance(chunks[-1], Response) else Response()
    else:
        response = await antigravity.non_stream_request(body)

    assert sent == accounts
    assert response.status_code == statuses[-1]
    if statuses[-1] != 200:
        assert response.headers["Retry-After"] == "17"
    assert all(
        call.args[3] is None for call in errors.await_args_list if call.args[2] == 503
    )
    recorder.record.assert_awaited_once()
    billed = recorder.record.await_args.kwargs
    assert billed["credential_name"] == accounts[-1]
    if state is None:
        assert "text_account_retry" not in billed
    else:
        trace = billed["text_account_retry"]
        assert trace["state"] == state
        assert [attempt["credential_name"] for attempt in trace["attempts"]] == accounts
        assert [attempt["status_code"] for attempt in trace["attempts"]] == statuses
        assert (trace["attempts"][-1].get("kind") == "same_account") == (
            accounts[-1] == accounts[-2]
        )


async def test_config_reload(monkeypatch):
    """通过面板保存后，下一个请求立即使用新规则；非法输入不覆盖旧值。"""
    values = {}
    key = "text_account_retry_status_codes"

    async def save(name, value):
        values[name] = value
        return True

    store = SimpleNamespace(
        _backend=None,
        set_config=save,
        get_all_config=AsyncMock(side_effect=values.copy),
    )
    for module in (storage_adapter, config_routes):
        monkeypatch.setattr(
            module,
            "get_storage_adapter",
            AsyncMock(return_value=store),
        )
    monkeypatch.setattr(config, "_config_cache", {})
    monkeypatch.setattr(config, "_config_initialized", True)
    assert (await utils.get_retry_config())["text_codes"] == [429, 502, 503]
    for raw, wanted in [("503, 502,503", [503, 502]), ("", [])]:
        request = config_routes.ConfigSaveRequest(config={key: raw})
        await config_routes.save_config(request, "test")
        assert (await utils.get_retry_config())["text_codes"] == wanted
    with pytest.raises(HTTPException) as error:
        await config_routes.save_config(
            config_routes.ConfigSaveRequest(config={key: "502,200"}), "test"
        )
    assert error.value.status_code == 400
    assert values[key] == []


async def test_credential_alias():
    """同一邮箱的另一份凭证不能充当备选账号。"""
    select = AsyncMock(
        side_effect=[("alias", {"user_email": "ME@EXAMPLE.COM"}), ("b", {})],
    )
    candidate = await text_retry.alternate_account(
        SimpleNamespace(get_valid_credential=select),
        "geminicli",
        "model",
        "a",
        {"user_email": "me@example.com"},
        0,
    )
    assert candidate == ("b", {})
    assert select.await_args.kwargs["exclude_filenames"] == {"a", "alias"}


@pytest.mark.parametrize("channel", [geminicli, antigravity])
@pytest.mark.parametrize("failure", ["status", "disconnect"])
async def test_partial_stream_never_replays(channel, failure, monkeypatch):
    """首块内容下发后，上游报错或断开都不能重放请求。"""
    _patch_common(monkeypatch, [])
    recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr(
        antigravity, "get_billing_recorder", AsyncMock(return_value=recorder)
    )
    monkeypatch.setattr(
        geminicli, "get_code_assist_endpoint", AsyncMock(return_value="http://test")
    )
    monkeypatch.setattr(channel, "get_auto_ban_error_codes", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        channel,
        "get_retry_config",
        AsyncMock(
            return_value={
                "text_codes": [429, 502],
                "retry_enabled": True,
                "max_retries": 5,
                "retry_interval": 0,
            }
        ),
    )
    choose = AsyncMock(
        return_value=("a", {"project_id": "project", "access_token": "token"}),
    )
    monkeypatch.setattr(channel.credential_manager, "get_valid_credential", choose)
    monkeypatch.setattr(channel, "record_api_call_success", AsyncMock())
    monkeypatch.setattr(channel, "record_api_call_error", AsyncMock())
    calls = []

    async def stream(**kwargs):
        calls.append(kwargs["headers"]["Authorization"])
        yield "data: first\n\n"
        if failure == "status":
            yield Response(b"failure", 502)
        else:
            raise ConnectionError("disconnected")

    monkeypatch.setattr(channel, "stream_post_async", stream)
    body = {"model": "gemini-2.5-flash", "request": {}}
    result = [part async for part in channel.stream_request(body)]
    assert calls == ["Bearer token"]
    assert result.count("data: first\n\n") == 1
    assert choose.await_count == 1
    if channel is antigravity:
        recorder.record.assert_awaited_once()
        assert "text_account_retry" not in recorder.record.await_args.kwargs


@pytest.mark.parametrize(
    "generation",
    [{"responseModalities": ["IMAGE"]}, {"imageConfig": {"aspectRatio": "1:1"}}],
)
async def test_image_bypasses_text_retry(generation, monkeypatch):
    """即使模型名不含 image，图片输出也不能进入文本换号规则。"""
    body = {"model": "gemini-2.5-flash", "request": {"generationConfig": generation}}
    image_handler = AsyncMock(return_value=Response(status_code=502))
    monkeypatch.setattr(antigravity, "_non_stream_image_request", image_handler)
    assert (await antigravity.non_stream_request(body)).status_code == 502
    image_handler.assert_awaited_once()
    monkeypatch.setattr(
        geminicli, "get_code_assist_endpoint", AsyncMock(return_value="http://test")
    )
    monkeypatch.setattr(
        geminicli,
        "get_auto_ban_error_codes",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        geminicli,
        "get_retry_config",
        AsyncMock(
            return_value={
                "text_codes": [502],
                "retry_enabled": True,
                "max_retries": 5,
                "retry_interval": 0,
            }
        ),
    )
    select = AsyncMock(return_value=("a", {"access_token": "a", "project_id": "p"}))
    monkeypatch.setattr(geminicli.credential_manager, "get_valid_credential", select)
    post = AsyncMock(return_value=httpx.Response(502, text="image error"))
    monkeypatch.setattr(geminicli, "post_async", post)
    monkeypatch.setattr(geminicli, "record_api_call_error", AsyncMock())
    assert (await geminicli.non_stream_request(body)).status_code == 502
    select.assert_awaited_once()
    post.assert_awaited_once()


@pytest.mark.parametrize(
    "value",
    ["400", "401", "403", "429,600", "502.0", True, [None]],
)
def test_invalid_codes(value):
    """认证与请求校验由原策略处理，非法状态码不能进入配置。"""
    with pytest.raises((TypeError, ValueError)):
        text_retry.parse_status_codes(value)
