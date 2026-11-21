"""无显式 sessionId 时的会话复用（request_step 不再恒为 1）。

背景：业务调用方（pydantic-ai）从不发送 ``sessionId``，旧逻辑对每个请求生成
随机 key，于是 ``conversation_id``/``trajectory_id`` 每条都换、``request_step``
100% 为 1。真实 Antigravity 客户端是多步 agent 会话，这个形态本身就是机器特征。

新语义：按 `账号 × 调用方 × 模型 × 时间桶` 复用一条 trajectory，
比例由 ``ANTIGRAVITY_SESSION_REUSE_RATIO`` 控制（默认 0 = 旧行为），
单条 trajectory 超过 ``ANTIGRAVITY_SESSION_MAX_STEPS`` 步后强制新开。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src import claude_routing
from src.api import antigravity as agy


@pytest.fixture(autouse=True)
def memory_store(monkeypatch):
    monkeypatch.setattr(agy, "_session_states", {})
    monkeypatch.setattr(agy, "_session_locks", {})
    monkeypatch.setattr(agy, "_get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(claude_routing, "current_api_key_id", lambda: "caller-a")
    monkeypatch.setenv("ANTIGRAVITY_SESSION_BUCKET_SECONDS", "3600")
    monkeypatch.setenv("ANTIGRAVITY_SESSION_MAX_STEPS", "30")
    yield


REQUEST = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}


@pytest.mark.asyncio
async def test_ratio_zero_keeps_one_trajectory_per_request(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_SESSION_REUSE_RATIO", "0")
    a = await agy._get_session_state(REQUEST, "m", "account")
    b = await agy._get_session_state(REQUEST, "m", "account")
    assert a.conversation_id != b.conversation_id
    assert (a.step_index, b.step_index) == (1, 1)
    assert not agy._session_states


@pytest.mark.asyncio
async def test_ratio_one_reuses_trajectory_and_increments_step(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_SESSION_REUSE_RATIO", "1")
    a = await agy._get_session_state(REQUEST, "m", "account")
    b = await agy._get_session_state(REQUEST, "m", "account")
    c = await agy._get_session_state(REQUEST, "m", "account")

    assert a.conversation_id == b.conversation_id == c.conversation_id
    assert a.trajectory_id == b.trajectory_id == c.trajectory_id
    assert (a.step_index, b.step_index, c.step_index) == (1, 2, 3)
    assert a.session_id == b.session_id  # installation identity stays stable


@pytest.mark.asyncio
async def test_reuse_is_scoped_by_account_model_and_caller(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_SESSION_REUSE_RATIO", "1")
    base = await agy._get_session_state(REQUEST, "m", "account-a")
    other_account = await agy._get_session_state(REQUEST, "m", "account-b")
    other_model = await agy._get_session_state(REQUEST, "m2", "account-a")

    assert base.trajectory_id != other_account.trajectory_id
    assert base.trajectory_id != other_model.trajectory_id

    monkeypatch.setattr(claude_routing, "current_api_key_id", lambda: "caller-b")
    other_caller = await agy._get_session_state(REQUEST, "m", "account-a")
    assert other_caller.trajectory_id != base.trajectory_id


@pytest.mark.asyncio
async def test_new_bucket_starts_a_new_trajectory(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_SESSION_REUSE_RATIO", "1")
    monkeypatch.setenv("ANTIGRAVITY_SESSION_BUCKET_SECONDS", "60")

    clock = [1_000_000.0]
    monkeypatch.setattr(agy.time, "time", lambda: clock[0])

    first = await agy._get_session_state(REQUEST, "m", "account")
    second = await agy._get_session_state(REQUEST, "m", "account")
    assert first.trajectory_id == second.trajectory_id
    assert second.step_index == 2

    clock[0] += 120  # 跨桶
    third = await agy._get_session_state(REQUEST, "m", "account")
    assert third.trajectory_id != first.trajectory_id
    assert third.step_index == 1


@pytest.mark.asyncio
async def test_step_cap_restarts_trajectory(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_SESSION_REUSE_RATIO", "1")
    monkeypatch.setenv("ANTIGRAVITY_SESSION_MAX_STEPS", "3")

    steps, trajectories = [], set()
    for _ in range(5):
        state = await agy._get_session_state(REQUEST, "m", "account")
        steps.append(state.step_index)
        trajectories.add(state.trajectory_id)

    assert steps == [1, 2, 3, 1, 2], steps
    assert len(trajectories) == 2, "超过步数上限必须换 trajectory"


@pytest.mark.asyncio
async def test_partial_ratio_splits_traffic(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_SESSION_REUSE_RATIO", "0.5")
    keys = set()
    for i in range(200):
        key = agy._session_key(
            {"contents": []}, "m", f"account-{i}", reuse_ratio=0.5
        )
        keys.add(key.split(":", 1)[0])
    assert "derived" in keys and "request" in keys, "比例应同时产出复用与非复用两组"


@pytest.mark.asyncio
async def test_concurrent_allocation_yields_unique_steps(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_SESSION_REUSE_RATIO", "1")
    states = await asyncio.gather(
        *(agy._get_session_state(REQUEST, "m", "account") for _ in range(20))
    )
    assert sorted(s.step_index for s in states) == list(range(1, 21))
    assert len({s.trajectory_id for s in states}) == 1
