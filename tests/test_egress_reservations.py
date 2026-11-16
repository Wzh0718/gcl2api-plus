import sqlite3
from unittest.mock import patch

import pytest
import pytest_asyncio

from src.storage.sqlite_manager import SQLiteManager


@pytest_asyncio.fixture
async def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    db = SQLiteManager()
    await db.initialize()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_deleted_exit_expires_exactly_seven_days_later(storage):
    await storage.store_credential("old.json", {}, "antigravity")
    await storage.update_antigravity_account_health("old.json", egress_ip="192.0.2.1")
    with patch("src.storage.sqlite_manager.time.time", return_value=2000000000):
        assert await storage.delete_credential("old.json", "antigravity")
    with patch("src.storage.sqlite_manager.time.time", return_value=2000000000 + 7*86400 - 1):
        assert await storage.is_antigravity_egress_ip_in_use("new.json", "192.0.2.1")
        assert not await storage.delete_credential("old.json", "antigravity")
    with patch("src.storage.sqlite_manager.time.time", return_value=2000000000 + 7*86400):
        assert not await storage.is_antigravity_egress_ip_in_use("new.json", "192.0.2.1")


@pytest.mark.asyncio
async def test_existing_disabled_account_still_owns_exit(storage):
    await storage.store_credential("old.json", {}, "antigravity")
    await storage.update_credential_state("old.json", {"disabled": True}, "antigravity")
    await storage.update_antigravity_account_health("old.json", egress_ip="192.0.2.1")
    assert await storage.is_antigravity_egress_ip_in_use("new.json", "192.0.2.1")
    assert not await storage.is_antigravity_egress_ip_in_use("old.json", "192.0.2.1")


@pytest.mark.asyncio
async def test_legacy_orphan_gets_one_hold_not_renewed_on_restart(storage):
    with sqlite3.connect(storage._db_path) as conn:
        conn.execute("INSERT INTO antigravity_account_health(credential_name,egress_ip) VALUES (?,?)",
                     ("deleted.json", "192.0.2.1"))
    await storage.close()
    with patch("src.storage.sqlite_manager.time.time", return_value=2000000000):
        await storage.initialize()
        assert await storage.is_antigravity_egress_ip_in_use("new.json", "192.0.2.1")
    await storage.close()
    with patch("src.storage.sqlite_manager.time.time", return_value=2000000000 + 7*86400):
        await storage.initialize()
        assert not await storage.is_antigravity_egress_ip_in_use("new.json", "192.0.2.1")
    with sqlite3.connect(storage._db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM antigravity_account_health").fetchone()[0] == 0
