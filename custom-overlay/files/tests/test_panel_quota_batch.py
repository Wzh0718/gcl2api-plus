"""
凭证批量额度接口（额度模型选择器）测试。

覆盖 POST /creds/quota/batch 的逐凭证结果、TTL 缓存、参数校验，
以及重构后 GET /creds/quota/{filename} 的响应形状。
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.panel import creds as creds_module
from src.utils import verify_panel_token


def _fake_quota_result(filename: str) -> dict:
    if filename == "bad.json":
        return {"success": False, "filename": filename, "error": "boom"}
    return {
        "success": True,
        "filename": filename,
        "models": {
            "claude-sonnet-4-6": {"remaining": 0.5, "resetTime": "N/A", "resetTimeRaw": ""},
        },
        "groups": None,
    }


def _build_app(monkeypatch, calls: list) -> FastAPI:
    app = FastAPI()
    app.include_router(creds_module.router)
    app.dependency_overrides[verify_panel_token] = lambda: "test-token"

    async def fake_collect(filename: str, mode: str) -> dict:
        calls.append(filename)
        return _fake_quota_result(filename)

    monkeypatch.setattr(creds_module, "_collect_quota_for_filename", fake_collect)
    monkeypatch.setattr(creds_module, "_quota_batch_cache", {})
    return app


@pytest.mark.asyncio
async def test_batch_quota_returns_per_file_results(monkeypatch):
    calls: list = []
    app = _build_app(monkeypatch, calls)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/creds/quota/batch",
            json={"filenames": ["a.json", "bad.json"], "mode": "antigravity"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["results"]["a.json"]["success"] is True
    assert data["results"]["a.json"]["models"]["claude-sonnet-4-6"]["remaining"] == 0.5
    assert data["results"]["bad.json"] == {
        "success": False,
        "filename": "bad.json",
        "error": "boom",
    }
    assert sorted(calls) == ["a.json", "bad.json"]


@pytest.mark.asyncio
async def test_batch_quota_uses_ttl_cache(monkeypatch):
    calls: list = []
    app = _build_app(monkeypatch, calls)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/creds/quota/batch",
            json={"filenames": ["a.json", "b.json"], "mode": "antigravity"},
        )
        second = await client.post(
            "/creds/quota/batch",
            json={"filenames": ["a.json", "b.json"], "mode": "antigravity"},
        )

    assert first.status_code == second.status_code == 200
    # 第二次命中缓存：不再触发上游采集
    assert calls == ["a.json", "b.json"]
    assert second.json()["results"] == first.json()["results"]


@pytest.mark.asyncio
async def test_batch_quota_rejects_empty_and_oversized_requests(monkeypatch):
    calls: list = []
    app = _build_app(monkeypatch, calls)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        empty = await client.post("/creds/quota/batch", json={"filenames": []})
        oversized = await client.post(
            "/creds/quota/batch",
            json={
                "filenames": [
                    f"{i}.json" for i in range(creds_module.QUOTA_BATCH_MAX_FILES + 1)
                ]
            },
        )

    assert empty.status_code == 400
    assert oversized.status_code == 400
    assert calls == []


@pytest.mark.asyncio
async def test_single_quota_endpoint_returns_result_shape(monkeypatch):
    calls: list = []
    app = _build_app(monkeypatch, calls)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.get("/creds/quota/a.json", params={"mode": "antigravity"})
        failed = await client.get("/creds/quota/bad.json", params={"mode": "antigravity"})

    assert ok.status_code == 200
    ok_data = ok.json()
    assert ok_data["success"] is True
    assert ok_data["filename"] == "a.json"
    assert "claude-sonnet-4-6" in ok_data["models"]

    assert failed.status_code == 400
    failed_data = failed.json()
    assert failed_data["success"] is False
    assert failed_data["error"] == "boom"
