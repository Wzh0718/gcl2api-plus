import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from src.api import antigravity as agy
from src import claude_routing

_real_get_redis = agy._get_redis


@pytest.fixture(autouse=True)
def session_store(monkeypatch):
    monkeypatch.setattr(agy, '_session_states', {})
    monkeypatch.setattr(agy, '_get_redis', AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_identical_openings_do_not_merge_independent_tasks():
    request = {'contents': [{'role': 'user', 'parts': [{'text': 'hello'}]}]}
    a = await agy._get_session_state(request, 'm', 'account')
    b = await agy._get_session_state(request, 'm', 'account')
    assert a.conversation_id != b.conversation_id
    assert a.trajectory_id != b.trajectory_id
    assert a.step_index == b.step_index == 1
    assert a.session_id == b.session_id  # account/installation identity is stable
    assert not agy._session_states  # no cache growth for uncorrelated requests


@pytest.mark.asyncio
async def test_explicit_continuation_returns_immutable_snapshots():
    a = await agy._get_session_state({'sessionId': 'conversation'}, 'm', 'account')
    b = await agy._get_session_state({'sessionId': 'conversation'}, 'm', 'account')
    assert a.conversation_id == b.conversation_id
    assert (a.step_index, b.step_index) == (1, 2)
    b.step_index = 999
    c = await agy._get_session_state({'sessionId': 'conversation'}, 'm', 'account')
    assert c.step_index == 3


def test_explicit_conversation_is_scoped_by_caller_model_and_account(monkeypatch):
    request = {'sessionId': 'same-id'}
    monkeypatch.setattr(claude_routing, 'current_api_key_id', lambda: 'caller-a')
    a = agy._session_key(request, 'm', 'account')
    assert a != agy._session_key(request, 'other', 'account')
    assert a != agy._session_key(request, 'm', 'other')
    monkeypatch.setattr(claude_routing, 'current_api_key_id', lambda: 'caller-b')
    assert a != agy._session_key(request, 'm', 'account')
    assert 'same-id' not in a


@pytest.mark.asyncio
async def test_redis_allocations_are_one_atomic_operation(monkeypatch):
    class Redis:
        def __init__(self):
            self.values = {}
            self.calls = 0

        async def eval(self, script, numkeys, key, initial, now, ttl, max_steps):
            await asyncio.sleep(0)  # allow all callers to reach allocation
            assert script == agy._SESSION_ALLOCATE_LUA
            assert numkeys == 1 and ttl == agy.SESSION_TTL_SECONDS
            assert int(max_steps) > 0
            self.calls += 1
            state = json.loads(self.values.get(key, initial))
            if key in self.values:
                state['step_index'] += 1
            self.values[key] = json.dumps(state)
            return self.values[key]

    redis = Redis()
    monkeypatch.setattr(agy, '_get_redis', AsyncMock(return_value=redis))
    states = await asyncio.gather(*(agy._get_session_state({'sessionId': 's'}, 'm', 'a') for _ in range(20)))
    assert sorted(s.step_index for s in states) == list(range(1, 21))
    assert len({s.trajectory_id for s in states}) == 1
    assert redis.calls == 20


@pytest.mark.asyncio
async def test_redis_failure_uses_independent_memory_trajectory(monkeypatch):
    class BrokenRedis:
        async def eval(self, *args):
            raise OSError('offline')
    monkeypatch.setattr(agy, '_get_redis', AsyncMock(return_value=BrokenRedis()))
    a = await agy._get_session_state({'sessionId': 's'}, 'm', 'a')
    b = await agy._get_session_state({'sessionId': 's'}, 'm', 'a')
    assert a.trajectory_id == b.trajectory_id
    assert (a.step_index, b.step_index) == (1, 2)


@pytest.mark.asyncio
async def test_caller_session_id_is_not_forwarded_across_accounts(monkeypatch):
    from src import antigravity_model_catalog as catalog
    monkeypatch.setattr(catalog, 'get_model_enum', lambda model: 'MODEL_TEST')
    monkeypatch.setattr(catalog, 'get_model_info', lambda model: {})
    request = {'sessionId': 'caller-conversation', 'contents': [{'role': 'user', 'parts': [{'text': 'hello'}]}]}
    a, _ = await agy.wrap_cli_request(request, 'gemini-test', 'project', credential_key='a', skip_prompt_fusion=True)
    b, _ = await agy.wrap_cli_request(request, 'gemini-test', 'project', credential_key='b', skip_prompt_fusion=True)
    assert a['request']['sessionId'] != b['request']['sessionId']
    assert a['request']['sessionId'] != 'caller-conversation'
    assert request['sessionId'] == 'caller-conversation'


@pytest.mark.asyncio
async def test_redis_initialization_is_shared_before_any_fallback(monkeypatch):
    from types import SimpleNamespace
    entered, release = asyncio.Event(), asyncio.Event()
    async def ping():
        entered.set()
        await release.wait()
    client = SimpleNamespace(ping=AsyncMock(side_effect=ping), aclose=AsyncMock())
    monkeypatch.setattr(agy, '_redis_checked', False)
    monkeypatch.setattr(agy, '_redis_client', None)
    monkeypatch.setattr(agy, '_redis_init_lock', asyncio.Lock())
    monkeypatch.setattr(agy, 'create_redis_client_from_env', lambda **kwargs: client)
    first = asyncio.create_task(_real_get_redis())
    await entered.wait()
    others = [asyncio.create_task(_real_get_redis()) for _ in range(10)]
    await asyncio.sleep(0)
    assert all(not task.done() for task in others)
    release.set()
    results = await asyncio.gather(first, *others)
    assert all(result is client for result in results)
    client.ping.assert_awaited_once()
