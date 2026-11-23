"""Shared upgrade and accounting contract. External DSNs must point to disposable databases."""
import asyncio
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from src.storage_adapter import StorageAdapter
from src.api_keys import ApiKeyService


@pytest.mark.parametrize("backend", ["sqlite", "postgresql", "mysql"])
async def test_upgrade_concurrent_accounting_and_restart(backend, tmp_path, monkeypatch):
    from src.storage.sqlite_pragmas import connect

    if backend != "sqlite":
        setting = "MAIN_MYSQL_DSN" if backend == "mysql" else "MAIN_POSTGRES_DSN"
        dsn = os.getenv("TEST_" + setting)
        if not dsn:
            pytest.skip("Requires TEST_" + setting)
        monkeypatch.setenv(setting, dsn)
    # No backend selection is injected into the upgraded service.
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("MAIN_POSTGRES_SCHEMA", "compat_" + uuid4().hex)
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.delenv("MODEL_STATS_POSTGRES_DSN", raising=False)
    prefix = uuid4().hex
    day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    marker = prefix + ".json"
    seed_args = dict(billing_date=day, credential_name=marker, model="compat", currency="CNY",
                     input_tokens=2, output_tokens=3, cache_tokens=0, thought_tokens=0, total_tokens=5,
                     input_cost="0.1", output_cost="0.00000001", cache_cost="0",
                     total_cost="0.10000001", success=True, unknown_usage=False, dedupe_ttl_seconds=600)
    legacy = os.getenv("TEST_LEGACY_MAIN_SOURCE")
    seed = StorageAdapter()
    if legacy and backend != "mysql":
        # Run the previous release in its own interpreter, using only its checkout.
        import json
        script = """
import asyncio,json,sys
from src.api_keys import ApiKeyService
from src.storage_adapter import StorageAdapter
async def seed():
    args=json.loads(sys.stdin.read())
    s=StorageAdapter()
    await s.initialize()
    assert await s.store_credential(args['credential_name'], {'project_id':'legacy'})
    record=await ApiKeyService(s).create_key(name=args['credential_name'])
    key=record['item']['id']
    assert await s.record_billing_usage(api_key_id=key, **args)
    await s.close()
    print(key)
asyncio.run(seed())
"""
        env = {**os.environ, "MAIN_STORAGE_BACKEND": backend, "PYTHONPATH": str(Path(legacy).resolve())}
        result = subprocess.run([sys.executable, "-c", script], cwd=legacy, env=env,
                                input=json.dumps(dict(seed_args, request_id=prefix)), text=True,
                                capture_output=True, check=True)
        key = result.stdout.splitlines()[-1]
    else:
        await seed.initialize()
        try:
            assert await seed.store_credential(marker, {"project_id": "legacy"})
            key = (await ApiKeyService(seed).create_key(name=marker))["item"]["id"]
            assert await seed.record_billing_usage(request_id=prefix, api_key_id=key, **seed_args)
        finally:
            await seed.close()

    stores = [StorageAdapter(), StorageAdapter()]
    try:
        await asyncio.gather(*(s.initialize() for s in stores))
        assert all(s.get_backend_type() == backend for s in stores)
        assert (await stores[0].get_credential(marker))["project_id"] == "legacy"
        assert not await stores[1].record_billing_usage(request_id=prefix, api_key_id=key, **seed_args)
        # Two independently initialized managers race with duplicate and distinct IDs.
        results = await asyncio.gather(*(stores[i % 2].record_billing_usage(
            request_id=f"{prefix}-{i % 7}", api_key_id=key, **seed_args) for i in range(28)))
        assert sum(results) == 7
        # Force a NOT NULL failure after the claim. The same ID must still settle on retry.
        retry_id = prefix + "-retry"
        with pytest.raises(Exception):
            await stores[0].record_billing_usage(request_id=retry_id, api_key_id=key, **dict(seed_args, currency=None))
        assert await stores[1].record_billing_usage(request_id=retry_id, api_key_id=key, **seed_args)
        assert not await stores[0].record_billing_usage(request_id=retry_id, api_key_id=key, **seed_args)
        row = await stores[0].get_api_key_record(key)
        assert (row["quota_used"], row["quota_tokens_used"]) == ("0.90000009", 45)
        async with connect(stores[1]._backend._db_path) as db:
            async with db.execute("SELECT success_count,total_tokens,total_cost FROM billing_daily WHERE api_key_id=?", (key,)) as cur:
                assert tuple(await cur.fetchone()) == (9, 45, "0.90000009")
            async with db.execute("SELECT COUNT(*) FROM billing_request_dedupe WHERE request_id LIKE ?", (prefix + "%",)) as cur:
                assert (await cur.fetchone())[0] == 9
    finally:
        await asyncio.gather(*(s.close() for s in stores))
