import hashlib
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient


SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.asyncio
async def test_old_billing_rows_are_migrated_to_environment_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    db_path = tmp_path / "credentials.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE billing_daily (
                billing_date TEXT NOT NULL,
                credential_name TEXT NOT NULL,
                model TEXT NOT NULL,
                currency TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_tokens INTEGER NOT NULL DEFAULT 0,
                thought_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_cost TEXT NOT NULL DEFAULT '0.00000000',
                output_cost TEXT NOT NULL DEFAULT '0.00000000',
                cache_cost TEXT NOT NULL DEFAULT '0.00000000',
                total_cost TEXT NOT NULL DEFAULT '0.00000000',
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                unknown_usage_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT (unixepoch()),
                PRIMARY KEY (billing_date, credential_name, model, currency)
            )"""
        )
        await db.execute(
            """INSERT INTO billing_daily(
                billing_date, credential_name, model, currency,
                input_tokens, total_tokens, input_cost, total_cost, success_count
            ) VALUES ('2026-07-27', 'old.json', 'm', 'CNY', 10, 10, '1.00000000', '1.00000000', 1)"""
        )
        await db.commit()

    manager = SQLiteManager()
    await manager.initialize()

    async with aiosqlite.connect(manager._db_path) as db:
        columns = {
            row[1]: row[5]
            for row in await (await db.execute("PRAGMA table_info(billing_daily)")).fetchall()
        }
        row = await (
            await db.execute(
                "SELECT api_key_id, total_tokens, total_cost FROM billing_daily"
            )
        ).fetchone()
        environment_key = await (
            await db.execute(
                "SELECT id, kind, secret_hash FROM api_keys WHERE id = 'env'"
            )
        ).fetchone()

    assert columns["api_key_id"] == 2
    assert row == ("env", 10, "1.00000000")
    assert environment_key == ("env", "environment", None)
    await manager.close()


@pytest.mark.asyncio
async def test_created_key_can_be_retrieved_later_without_storing_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.api_keys import ApiKeyService
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    service = ApiKeyService(manager)

    created = await service.create_key(
        name="customer-a",
        quota_amount="100.00",
        reset_mode="manual",
        expires_at=None,
    )

    assert created["api_key"].startswith(f"gcli_{created['item']['key_prefix']}_")
    assert await service.get_secret(created["item"]["id"]) == created["api_key"]
    managed = next(
        item
        for item in (await service.list_keys())["items"]
        if item["id"] == created["item"]["id"]
    )
    assert managed["secret_available"] is True
    assert "api_key" not in managed
    assert "secret_hash" not in managed
    assert "secret_ciphertext" not in managed

    async with aiosqlite.connect(manager._db_path) as db:
        stored = await (
            await db.execute(
                "SELECT secret_hash, secret_ciphertext FROM api_keys WHERE id = ?",
                (created["item"]["id"],),
            )
        ).fetchone()
        serialized = "\n".join(
            str(value)
            for row in await (await db.execute("SELECT * FROM api_keys")).fetchall()
            for value in row
        )

    assert stored[0] == hashlib.sha256(created["api_key"].encode()).hexdigest()
    assert stored[1]
    assert stored[1] != created["api_key"]
    assert created["api_key"] not in serialized
    key_file = tmp_path / "api_keys.fernet.key"
    assert key_file.exists()
    assert key_file.stat().st_mode & 0o777 == 0o600
    await manager.close()


@pytest.mark.asyncio
async def test_old_hashed_only_key_is_marked_unrecoverable(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.api_keys import ApiKeyService
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    service = ApiKeyService(manager)
    created = await service.create_key(
        name="old-key", quota_amount=None, reset_mode="manual", expires_at=None
    )
    async with aiosqlite.connect(manager._db_path) as db:
        await db.execute(
            "UPDATE api_keys SET secret_ciphertext = NULL WHERE id = ?",
            (created["item"]["id"],),
        )
        await db.commit()

    item = await service.get_key(created["item"]["id"])
    assert item["secret_available"] is False
    with pytest.raises(ValueError, match="旧密钥.*重新创建"):
        await service.get_secret(created["item"]["id"])
    await manager.close()


@pytest.mark.asyncio
async def test_existing_api_key_table_gets_encrypted_secret_column(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    db_path = tmp_path / "credentials.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                key_prefix TEXT NOT NULL UNIQUE,
                secret_hash TEXT UNIQUE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                quota_amount TEXT,
                quota_used TEXT NOT NULL DEFAULT '0.00000000',
                currency TEXT NOT NULL DEFAULT 'CNY',
                reset_mode TEXT NOT NULL DEFAULT 'manual',
                quota_period_start TEXT NOT NULL,
                expires_at REAL,
                last_used_at REAL,
                last_reset_at REAL,
                revoked_at REAL,
                created_at REAL NOT NULL DEFAULT (unixepoch()),
                updated_at REAL NOT NULL DEFAULT (unixepoch())
            )"""
        )
        await db.commit()

    manager = SQLiteManager()
    await manager.initialize()
    async with aiosqlite.connect(manager._db_path) as db:
        columns = {
            row[1]
            for row in await (await db.execute("PRAGMA table_info(api_keys)")).fetchall()
        }

    assert "secret_ciphertext" in columns
    assert "quota_tokens_used" in columns
    await manager.close()


@pytest.mark.asyncio
async def test_key_authentication_enforces_status_expiry_and_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.api_keys import ApiKeyAuthError, ApiKeyService
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    service = ApiKeyService(manager)
    created = await service.create_key(
        name="limited",
        quota_amount="1.00",
        reset_mode="manual",
        expires_at=None,
    )

    principal = await service.authenticate(created["api_key"])
    assert principal.api_key_id == created["item"]["id"]

    await service.update_key(created["item"]["id"], status="disabled")
    with pytest.raises(ApiKeyAuthError) as disabled:
        await service.authenticate(created["api_key"])
    assert disabled.value.status_code == 403

    await service.update_key(
        created["item"]["id"],
        status="active",
        expires_at="2026-07-27T00:00:00+08:00",
    )
    with pytest.raises(ApiKeyAuthError) as expired:
        await service.authenticate(
            created["api_key"], now=datetime(2026, 7, 28, 0, 0, tzinfo=SHANGHAI)
        )
    assert expired.value.status_code == 403

    await service.update_key(created["item"]["id"], expires_at=None)
    await manager.set_api_key_quota_used(created["item"]["id"], "1.00000000")
    with pytest.raises(ApiKeyAuthError) as exhausted:
        await service.authenticate(created["api_key"])
    assert exhausted.value.status_code == 429
    await manager.close()


@pytest.mark.asyncio
async def test_billing_charges_key_once_and_manual_reset_preserves_history(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.api_keys import ApiKeyService
    from src.billing import BillingRecorder
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    service = ApiKeyService(manager)
    created = await service.create_key(
        name="billable",
        quota_amount="5.00",
        reset_mode="manual",
        expires_at=None,
    )
    await manager.upsert_billing_price("default", "1", "0", "0", "CNY")
    recorder = BillingRecorder(manager)

    for _ in range(2):
        await recorder.record(
            request_id="one-request",
            api_key_id=created["item"]["id"],
            credential_name="account.json",
            model="m",
            usage_metadata={"promptTokenCount": 1_000_000},
            success=True,
        )

    before = await service.get_key(created["item"]["id"])
    assert Decimal(before["quota_used"]) == Decimal("1.00000000")
    assert Decimal(before["quota_remaining"]) == Decimal("4.00000000")
    assert before["quota_tokens_used"] == 1_000_000

    reset = await service.reset_quota(created["item"]["id"])
    summary = await manager.get_billing_summary("today", api_key_id=created["item"]["id"])

    assert reset["quota_used"] == "0.00000000"
    assert reset["quota_tokens_used"] == 0
    assert summary["total_cost"] == "1.00000000"
    assert summary["success_count"] == 1
    await manager.close()


@pytest.mark.asyncio
async def test_environment_key_tracks_unlimited_token_and_cost_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.api_keys import ApiKeyService
    from src.billing import BillingRecorder
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    await manager.upsert_billing_price("default", "1", "0", "0", "CNY")

    await BillingRecorder(manager).record(
        request_id="environment-request",
        api_key_id="env",
        credential_name="account.json",
        model="m",
        usage_metadata={"promptTokenCount": 520_000},
        success=True,
    )

    item = await ApiKeyService(manager).get_key("env")
    assert item["quota_amount"] is None
    assert item["quota_tokens_used"] == 520_000
    assert item["quota_used"] == "0.52000000"
    await manager.close()


@pytest.mark.asyncio
async def test_monthly_quota_resets_on_first_shanghai_access(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.api_keys import ApiKeyService
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    service = ApiKeyService(manager)
    created = await service.create_key(
        name="monthly",
        quota_amount="20.00",
        reset_mode="monthly",
        expires_at=None,
        now=datetime(2026, 7, 28, 9, 0, tzinfo=SHANGHAI),
    )
    await manager.set_api_key_quota_used(created["item"]["id"], "7.00000000")
    await manager.set_api_key_quota_tokens_used(created["item"]["id"], 7_500_000)

    principal = await service.authenticate(
        created["api_key"],
        now=datetime(2026, 8, 1, 0, 0, tzinfo=SHANGHAI),
    )
    item = await service.get_key(
        created["item"]["id"],
        now=datetime(2026, 8, 1, 0, 0, tzinfo=SHANGHAI),
    )

    assert principal.api_key_id == created["item"]["id"]
    assert item["quota_used"] == "0.00000000"
    assert item["quota_tokens_used"] == 0
    assert item["quota_period_start"] == "2026-08-01"
    assert item["next_reset_at"] == "2026-09-01T00:00:00+08:00"
    await manager.close()


@pytest.mark.asyncio
async def test_switching_to_monthly_keeps_usage_until_next_month(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.api_keys import ApiKeyService
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    service = ApiKeyService(manager)
    created = await service.create_key(
        name="switch-reset-mode",
        quota_amount="10.00",
        reset_mode="manual",
        expires_at=None,
        now=datetime(2026, 7, 15, 12, 0, tzinfo=SHANGHAI),
    )
    await manager.set_api_key_quota_used(created["item"]["id"], "3.00000000")

    changed = await service.update_key(
        created["item"]["id"],
        reset_mode="monthly",
        now=datetime(2026, 7, 28, 12, 0, tzinfo=SHANGHAI),
    )
    same_month = await service.get_key(
        created["item"]["id"],
        now=datetime(2026, 7, 31, 23, 59, tzinfo=SHANGHAI),
    )
    next_month = await service.get_key(
        created["item"]["id"],
        now=datetime(2026, 8, 1, 0, 0, tzinfo=SHANGHAI),
    )

    assert changed["quota_used"] == "3.00000000"
    assert same_month["quota_used"] == "3.00000000"
    assert next_month["quota_used"] == "0.00000000"
    await manager.close()


@pytest.mark.asyncio
async def test_panel_key_api_retrieves_secret_without_exposing_it_in_lists(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("PANEL_PASSWORD", "panel-test")
    import src.storage_adapter as storage_module
    from src.panel.api_keys import router

    await storage_module.close_storage_adapter()
    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer panel-test"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api-keys",
            headers=headers,
            json={"name": "panel-key", "quota_amount": "9.50", "reset_mode": "manual"},
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["api_key"].startswith("gcli_")

        listed = await client.get("/api-keys", headers=headers)
        assert listed.status_code == 200
        managed = next(item for item in listed.json()["items"] if item["name"] == "panel-key")
        assert "api_key" not in managed
        assert "secret_hash" not in managed
        assert "secret_ciphertext" not in managed
        assert managed["secret_available"] is True

        secret = await client.get(
            f"/api-keys/{managed['id']}/secret", headers=headers
        )
        assert secret.status_code == 200
        assert secret.headers["cache-control"] == "no-store"
        assert secret.json() == {"api_key": payload["api_key"]}

        unauthorized_secret = await client.get(
            f"/api-keys/{managed['id']}/secret"
        )
        assert unauthorized_secret.status_code in {401, 403}

        reset = await client.post(
            f"/api-keys/{managed['id']}/quota-resets", headers=headers
        )
        assert reset.status_code == 200

        environment_update = await client.patch(
            "/api-keys/env", headers=headers, json={"status": "disabled"}
        )
        assert environment_update.status_code == 409
    await storage_module.close_storage_adapter()


@pytest.mark.asyncio
async def test_antigravity_auth_accepts_managed_and_environment_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setenv("API_PASSWORD", "legacy-environment-key")
    import src.storage_adapter as storage_module
    from src.api_keys import ApiKeyPrincipal, ApiKeyService
    from src.utils import authenticate_antigravity_key

    await storage_module.close_storage_adapter()
    storage = await storage_module.get_storage_adapter()
    created = await ApiKeyService(storage).create_key(
        name="route-key", quota_amount="10", reset_mode="manual", expires_at=None
    )

    app = FastAPI()

    @app.get("/antigravity/test")
    async def protected(principal: ApiKeyPrincipal = Depends(authenticate_antigravity_key)):
        return {"api_key_id": principal.api_key_id, "kind": principal.kind}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        managed = await client.get(
            "/antigravity/test", headers={"x-goog-api-key": created["api_key"]}
        )
        assert managed.status_code == 200
        assert managed.json() == {
            "api_key_id": created["item"]["id"],
            "kind": "managed",
        }

        environment = await client.get(
            "/antigravity/test",
            headers={"Authorization": "Bearer legacy-environment-key"},
        )
        assert environment.status_code == 200
        assert environment.json() == {"api_key_id": "env", "kind": "environment"}

        invalid = await client.get(
            "/antigravity/test", headers={"x-api-key": "invalid"}
        )
        assert invalid.status_code == 403
    await storage_module.close_storage_adapter()


@pytest.mark.asyncio
async def test_billing_can_rank_and_filter_by_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.api_keys import ApiKeyService
    from src.billing import BillingRecorder
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    service = ApiKeyService(manager)
    first = await service.create_key(
        name="first", quota_amount=None, reset_mode="manual", expires_at=None
    )
    second = await service.create_key(
        name="second", quota_amount=None, reset_mode="manual", expires_at=None
    )
    await manager.upsert_billing_price("default", "1", "0", "0", "CNY")
    recorder = BillingRecorder(manager)
    await recorder.record(
        request_id="first-1",
        api_key_id=first["item"]["id"],
        credential_name="one.json",
        model="m",
        usage_metadata={"promptTokenCount": 2_000_000},
        success=True,
    )
    await recorder.record(
        request_id="second-1",
        api_key_id=second["item"]["id"],
        credential_name="two.json",
        model="m",
        usage_metadata={"promptTokenCount": 1_000_000},
        success=True,
    )

    ranked = await manager.get_billing_keys("today", 1, 10)
    filtered = await manager.get_billing_summary(
        "today", api_key_id=second["item"]["id"]
    )
    accounts = await manager.get_billing_accounts(
        "today", 1, 10, api_key_id=first["item"]["id"]
    )

    assert [item["api_key_name"] for item in ranked["items"]] == ["first", "second"]
    assert filtered["total_cost"] == "1.00000000"
    assert [item["credential_name"] for item in accounts["items"]] == ["one.json"]
    await manager.close()


def test_antigravity_request_functions_expose_explicit_api_key_context():
    import inspect
    from src.api.antigravity import non_stream_request, stream_request

    assert "api_key_id" in inspect.signature(non_stream_request).parameters
    assert "api_key_id" in inspect.signature(stream_request).parameters
