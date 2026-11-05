import asyncio
import json

import pytest

from src.request_features import (
    RequestContentMiddleware, build_request_features, content_snapshot, request_content,
)


def test_protocol_snapshots_and_stable_fingerprints():
    payload = {
        "messages": [{"role": "system", "content": "规则"},
                     {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                     {"role": "tool", "tool_call_id": "t", "content": "result"}],
        "tools": [{"type": "function", "function": {"name": "search", "parameters": {}}}],
        "temperature": 0.5,
    }
    token = request_content.set(content_snapshot(payload))
    try:
        features, details = build_request_features({"request": {"contents": [], "systemInstruction": {"parts": [{"text": "upstream"}]}}})
        assert details["client"]["messages"] == payload["messages"]
        assert details["client"]["tools"] == payload["tools"]
        assert features["client"]["message_count"] == 3
        assert features["client"]["tool_count"] == 1
        first = features["client"]
        request_content.set(content_snapshot(dict(reversed(list(payload.items())))))
        assert build_request_features()[0]["client"] == first
        payload["tools"][0]["function"]["name"] = "lookup"
        request_content.set(content_snapshot(payload))
        changed = build_request_features()[0]["client"]
        assert changed["sha256"] != first["sha256"]
        assert changed["parts"]["system"] == first["parts"]["system"]
        assert changed["parts"]["tools"] != first["parts"]["tools"]
    finally:
        request_content.reset(token)
    assert content_snapshot({"system": "anthropic", "messages": [], "tools": []})["system"] == {"system": "anthropic"}
    assert content_snapshot({"instructions": "responses", "input": "hello"})["messages"] == "hello"


@pytest.mark.asyncio
async def test_asgi_streaming_context_isolation_and_body_preservation():
    observed = {}
    barrier = asyncio.Event()

    async def app(scope, receive, send):
        chunks = []
        while True:
            message = await receive()
            chunks.append(message["body"])
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        observed[scope["test_id"]] = json.loads(b"".join(chunks))
        if len(observed) == 2:
            barrier.set()
        await barrier.wait()
        # Simulate recording after response start in a streaming task.
        async def record():
            return build_request_features()[1]["client"]
        snapshot = await asyncio.create_task(record())
        assert snapshot["system"] == {"system": scope["test_id"]}
        await send({"type": "http.response.body", "body": b"done"})

    middleware = RequestContentMiddleware(app)

    async def run(name):
        raw = json.dumps({"system": name, "messages": [{"role": "user", "content": name}]}).encode()
        messages = iter([{"type": "http.request", "body": raw[:12], "more_body": True},
                         {"type": "http.request", "body": raw[12:]}])
        async def receive():
            return next(messages)
        async def send(message):
            pass
        await middleware({"type": "http", "method": "POST", "path": "/v1/messages",
                          "headers": [(b"content-type", b"application/json")], "test_id": name}, receive, send)
        assert request_content.get() is None
    await asyncio.wait_for(asyncio.gather(run("a"), run("b")), 2)
    assert observed["a"]["system"] == "a"
    assert observed["b"]["system"] == "b"


@pytest.mark.asyncio
async def test_content_persistence_auth_and_email(tmp_path, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from src.call_monitor import CallMonitor
    from src.panel import call_monitor as panel
    from src.storage_adapter import StorageAdapter

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    adapter = StorageAdapter()
    await adapter.initialize()
    try:
        backend = adapter._backend
        token = request_content.set(content_snapshot({"system": "秘密", "messages": [{"role": "user", "content": "原文"}], "tools": [{"name": "test"}]}))
        try:
            await CallMonitor(adapter).record(request_id="r-content", success=False, status_code=500,
                                               upstream_request={"contents": [{"role": "user", "parts": [{"text": "原文"}]}]})
        finally:
            request_content.reset(token)
        rows = await backend.list_call_records()
        assert rows[0]["request_features"]["client"]["message_count"] == 1
        assert "request_details" not in rows[0]
        assert "秘密" not in json.dumps(rows, ensure_ascii=False)
        detail = await backend.get_call_record_content("r-content")
        assert detail["request_details"]["client"]["system"] == {"system": "秘密"}
        assert detail["request_details"]["client"]["tools"] == [{"name": "test"}]
        assert "upstream" in detail["request_details"]

        async def storage():
            return adapter
        monkeypatch.setattr(panel, "get_storage_adapter", storage)
        app = FastAPI()
        app.include_router(panel.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/call-monitor/content?request_id=r-content")).status_code in {401, 403, 422}
            app.dependency_overrides[panel.verify_panel_token] = lambda: "ok"
            response = await client.get("/call-monitor/content?request_id=r-content")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == detail
            assert (await client.get("/call-monitor/content?request_id=missing")).status_code == 404

        class Accounts:
            async def get_all_credential_states(self, mode):
                return {"acc.json": {"user_email": "person@example.com"}}
        rows = [{"group_key": "acc.json"}, {"credential_name": "acc.json"}]
        await panel._attach_account_emails(rows, Accounts())
        assert all(row["user_email"] == "person@example.com" for row in rows)
        await backend.delete_call_records_before(10**12)
        assert await backend.get_call_record_content("r-content") is None
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_content_migration_preserves_old_records(tmp_path, monkeypatch):
    import aiosqlite
    from src.storage_adapter import StorageAdapter

    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    adapter = StorageAdapter()
    await adapter.initialize()
    backend = adapter._backend
    await backend.insert_call_record(request_id="legacy", created_at=1, success=True)
    async with aiosqlite.connect(backend._db_path) as db:
        await db.execute("ALTER TABLE call_records DROP COLUMN request_features")
        await db.execute("ALTER TABLE call_records DROP COLUMN request_details")
        await db.commit()
    await adapter.close()
    adapter = StorageAdapter()
    await adapter.initialize()
    try:
        rows = await adapter._backend.list_call_records()
        assert rows[0]["request_id"] == "legacy"
        assert rows[0]["request_features"] == {}
        assert (await adapter._backend.get_call_record_content("legacy"))["request_details"] == {}
    finally:
        await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('path,body', [('/auth/login', b'{"password":"do-not-capture"}'),
                                      ('/v1/messages', b'invalid-json')])
async def test_capture_excludes_panel_and_invalid_json_and_cleans_up(path, body):
    async def app(scope, receive, send):
        assert (await receive())["body"] == body
        assert request_content.get() is None
        raise RuntimeError("route failed")
    async def receive():
        return {"type": "http.request", "body": body}
    async def send(message):
        pass
    with pytest.raises(RuntimeError, match="route failed"):
        await RequestContentMiddleware(app)(
            {"type": "http", "method": "POST", "path": path,
             "headers": [(b"content-type", b"application/json")]}, receive, send)
    assert request_content.get() is None
