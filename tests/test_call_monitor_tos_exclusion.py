"""调用监控统计排除 TOS 封禁账号的脏数据。"""

import time

import pytest

TOS_REASON = (
    '{"error":{"code":403,"message":"This service has been disabled in this account '
    'for violation of Terms of Service. Please submit an appeal to continue using this '
    'product.","status":"PERMISSION_DENIED"}}'
)


async def _make_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage_adapter import StorageAdapter

    adapter = StorageAdapter()
    await adapter.initialize()
    return adapter


def _record_kwargs(request_id, *, credential_name, success, status_code):
    return {
        "request_id": request_id,
        "created_at": time.time(),
        "api_key_id": "key-1",
        "credential_name": credential_name,
        "channel": "antigravity",
        "model_name": "gemini-2.5-flash-lite",
        "task_type": "chat",
        "success": success,
        "status_code": status_code,
        "gateway_seconds": 1.0,
        "input_tokens": 10,
        "output_tokens": 10,
        "cache_tokens": 0,
        "thought_tokens": 0,
        "total_tokens": 20,
        "total_cost": "0.00010000",
        "currency": "CNY",
    }


@pytest.mark.asyncio
async def test_tos_banned_account_excluded_from_stats(tmp_path, monkeypatch):
    adapter = await _make_adapter(tmp_path, monkeypatch)
    backend = adapter._backend

    await backend.store_credential("banned.json", {"access_token": "t"}, mode="antigravity")
    await backend.update_credential_state(
        "banned.json", {"disabled": True, "error_codes": [403]}, mode="antigravity"
    )
    await backend.update_antigravity_account_health(
        "banned.json",
        eligibility_status="error",
        eligibility_reason=TOS_REASON,
        last_403_reason=TOS_REASON,
    )
    await backend.store_credential("healthy.json", {"access_token": "t"}, mode="antigravity")

    for i in range(5):
        await backend.insert_call_record(
            **_record_kwargs(f"banned-{i}", credential_name="banned.json", success=False, status_code=403)
        )
    await backend.insert_call_record(
        **_record_kwargs("ok-1", credential_name="healthy.json", success=True, status_code=200)
    )

    rows = await backend.aggregate_call_records(
        group_by="credential_name", since_ts=0.0
    )
    names = {row["group_key"] for row in rows}
    assert names == {"healthy.json"}

    counts = await backend.count_call_records(since_ts=0.0)
    assert counts["logs"] == 1
    assert counts["failed"] == 0

    await adapter.close()
