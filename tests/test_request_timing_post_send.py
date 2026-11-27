"""响应发完之后的收尾工作（计费）不能挡在客户端前面。

线上实测（2026-11-26，timing_id=0c6b1887…）：上游末包 1.864s 到达，客户端
1.900s 才收到 —— 中间 36ms 全在等计费写库（billing_pg_sql p50 5.8ms /
p90 12.4ms / p99 33ms）。改成「响应体发出后再写」。
"""

import pytest

from src import request_timing


@pytest.mark.asyncio
async def test_defer_after_send_without_timing_context_returns_false():
    """没有计时上下文（例如直接调用 handler 的测试）时退回同步执行。"""
    ran = []

    async def work():
        ran.append(True)

    assert request_timing.defer_after_send(work) is False
    assert ran == []


@pytest.mark.asyncio
async def test_defer_after_send_runs_callbacks_after_response():
    state = request_timing.RequestTiming()
    token = request_timing.request_timing.set(state)
    ran = []

    async def work():
        ran.append(True)

    try:
        assert request_timing.defer_after_send(work) is True
        assert state.post_send_callbacks == [work]
        assert ran == []  # 注册时不执行

        await request_timing._run_post_send_callbacks(state)
        assert ran == [True]
        assert state.post_send_callbacks == []  # 执行后清空，避免重复
    finally:
        request_timing.request_timing.reset(token)


@pytest.mark.asyncio
async def test_post_send_callback_failure_does_not_break_connection_teardown():
    state = request_timing.RequestTiming()
    token = request_timing.request_timing.set(state)
    ran = []

    async def boom():
        raise RuntimeError("billing down")

    async def ok():
        ran.append(True)

    try:
        request_timing.defer_after_send(boom)
        request_timing.defer_after_send(ok)
        await request_timing._run_post_send_callbacks(state)
    finally:
        request_timing.request_timing.reset(token)

    assert ran == [True], "前一个回调失败不能影响后续回调"
    assert state.post_send_callbacks == []


def test_post_send_callbacks_are_per_request():
    """两个请求的状态互不共享（避免把别人的计费丢进错误的收尾队列）。"""
    first, second = request_timing.RequestTiming(), request_timing.RequestTiming()
    assert first.post_send_callbacks is not second.post_send_callbacks


def test_middleware_runs_post_send_work_after_response_body():
    """端到端：handler 先返回，响应体发出后回调才执行。"""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    events = []

    async def handler(request):
        async def work():
            events.append("callback")

        assert request_timing.defer_after_send(work) is True
        events.append("handler_returned")
        return PlainTextResponse("ok")

    # 中间件只对 POST 的 /v1/* 等推理路径计时，用真实路径触发。
    app = Starlette(routes=[Route("/v1/chat/completions", handler, methods=["POST"])])
    app.add_middleware(request_timing.RequestTimingMiddleware)

    response = TestClient(app).post("/v1/chat/completions", json={"model": "m"})

    assert response.status_code == 200
    assert response.text == "ok"
    assert events == ["handler_returned", "callback"]
