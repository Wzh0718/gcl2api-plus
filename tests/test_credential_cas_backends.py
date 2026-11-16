"""Contract tests for non-SQLite conditional-write backends."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.storage.psql_manager import PSQLManager
from src.storage.mongodb_manager import MongoDBManager
from src.credential_revision import REVISION_KEY


class Acquire:
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize('matched', [True, False])
async def test_postgres_uses_one_conditional_update_and_no_insert(matched):
    conn = SimpleNamespace(execute=AsyncMock(return_value='UPDATE 1' if matched else 'UPDATE 0'))
    backend = PSQLManager()
    backend._initialized = True
    backend._pool = SimpleNamespace(acquire=lambda: Acquire(conn))
    expected = {'access_token': 'old', REVISION_KEY: 'generation-1'}
    result = await backend.compare_and_swap_credential('test.json', expected, {'access_token': 'new'},
                                                       'antigravity', state_updates={'disabled': True})
    assert result is matched
    sql, *params = conn.execute.await_args.args
    assert sql.startswith('UPDATE antigravity_credentials ')
    assert 'credential_data::jsonb =' in sql and 'filename =' in sql
    assert json.loads(params[-1]) == expected
    assert json.loads(params[0])[REVISION_KEY] != 'generation-1'
    assert json.loads(params[0])['access_token'] == 'new'
    assert 'old' not in sql and 'new' not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize('matched', [True, False])
async def test_mongodb_matches_snapshot_and_never_upserts(matched):
    collection = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=int(matched))))
    backend = MongoDBManager()
    backend._initialized = True
    backend._db = {'antigravity_credentials': collection}
    expected = {'access_token': 'old', REVISION_KEY: 'generation-1'}
    result = await backend.compare_and_swap_credential('test.json', expected, {'access_token': 'new'}, 'antigravity')
    assert result is matched
    query, update = collection.update_one.await_args.args
    assert query == {'filename': 'test.json', 'credential_data': expected}
    assert collection.update_one.await_args.kwargs['upsert'] is False
    assert update['$set']['credential_data'][REVISION_KEY] != 'generation-1'
    assert update['$set']['credential_data']['access_token'] == 'new'
