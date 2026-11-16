import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from src import operational_audit as audit
from src.request_capture import features
from src.storage.sqlite_manager import SQLiteManager


def test_journal_is_persistent_and_drops_unapproved_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    token = audit.refresh_context.set({"refresh_event_id": "operation-1"})
    try:
        audit.emit("refresh_started", account_ref=audit.reference("account"),
                   access_token="secret-token", prompt="private-input")
    finally:
        audit.refresh_context.reset(token)
    store = audit.CaptureStore(tmp_path / "operational-audit")
    rows = store.query("attempts", audit.POLICY)
    assert len(rows) == 1
    assert rows[0]["refresh_event_id"] == "operation-1"
    assert rows[0]["pid"] > 0 and rows[0]["instance_id"]
    assert "secret-token" not in json.dumps(rows) and "private-input" not in json.dumps(rows)


def test_audit_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(audit.CaptureStore, "write", lambda *a, **k: 1 / 0)
    audit.emit("test_failure")


def test_field_fingerprints_allow_consistency_checks_without_content():
    row = features({"requestId": "agent/conversation/123/trajectory/4", "request": {
        "contents": [{"parts": [{"text": "private-prompt"}]}],
        "labels": {"cascade_id": "conversation", "trajectory_id": "trajectory", "last_step_index": "3"}}})
    correlation = row["correlation"]
    assert correlation["cascade_id"] == correlation["request_conversation"]
    assert correlation["trajectory_id"] == correlation["request_trajectory"]
    assert correlation["request_step"] == correlation["last_step_index"] + 1
    assert "private-prompt" not in json.dumps(row)


@pytest.mark.asyncio
async def test_authorization_audit_is_atomic_and_records_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    db = SQLiteManager()
    await db.initialize()
    try:
        data = {"access_token": "secret-token", "refresh_token": "secret-rt"}
        assert await db.store_credential("a", data, "antigravity")
        old = await db.get_credential("a", "antigravity")
        assert await db.compare_and_swap_credential("a", old, {**old, "access_token": "next-token"}, "antigravity")
        assert not await db.compare_and_swap_credential("a", old, data, "antigravity")
        assert await db.delete_credential("a", "antigravity")
        async with aiosqlite.connect(db._db_path) as conn:
            rows = [json.loads(r[0]) for r in await (await conn.execute(
                "SELECT event_json FROM credential_audit_events ORDER BY id")).fetchall()]
        assert [r["outcome"] for r in rows] == ["committed", "committed", "revision_conflict", "committed"]
        assert rows[0]["new_revision_ref"] == rows[1]["old_revision_ref"]
        assert rows[1]["new_revision_ref"] == rows[3]["old_revision_ref"]
        assert not any(s in json.dumps(rows) for s in ("secret-token", "secret-rt", "next-token"))
        async def fail(*args, **kwargs):
            raise RuntimeError("audit unavailable")
        monkeypatch.setattr(db, "_audit_credential", fail)
        assert not await db.store_credential("a", data, "antigravity")
        assert await db.get_credential("a", "antigravity") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_refresh_events_distinguish_network_from_reuse(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.credential_manager import CredentialManager
    from src.google_oauth_api import Credentials
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    db = SQLiteManager()
    await db.initialize()
    manager = CredentialManager()
    manager._initialized = True
    manager._storage_adapter = SimpleNamespace(**{name: getattr(db, name) for name in (
        "get_credential", "compare_and_swap_credential", "acquire_refresh_lease",
        "renew_refresh_lease", "release_refresh_lease")},
        resolve_credential_network=AsyncMock(return_value={"proxy_mode": "direct"}))
    try:
        await db.store_credential("a", {"refresh_token": "private-rt"}, "antigravity")
        async def refresh(self):
            self.access_token = "private-at"
            self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        monkeypatch.setattr(Credentials, "refresh", refresh)
        old = await db.get_credential("a", "antigravity")
        assert await manager._refresh_token(old, "a", "antigravity")
        assert await manager._refresh_token(old, "a", "antigravity")
        rows = audit.CaptureStore(tmp_path / "operational-audit").query("attempts", audit.POLICY, limit=100)
        events = [r["event"] for r in rows]
        assert events.count("oauth_operation_started") == 1
        assert events.count("refresh_reused") == 1
        assert events.count("refresh_finished") == 2
        starts = {r["refresh_event_id"] for r in rows if r["event"] == "refresh_started"}
        ends = {r["refresh_event_id"] for r in rows if r["event"] == "refresh_finished"}
        assert starts == ends
        assert "private-rt" not in json.dumps(rows) and "private-at" not in json.dumps(rows)
    finally:
        await manager.close()
        await db.close()
