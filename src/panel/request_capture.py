"""Authenticated evidence status, filtered queries and bounded JSON export."""
import asyncio
import json
import math

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from src.capture_config import capture_policies, capture_settings
from src.request_capture import get_capture_store, maintain_capture
from src.storage.request_capture_store import POLICIES
from src.utils import verify_panel_token


router = APIRouter(prefix="/call-monitor/capture", tags=["call-monitor"])


@router.get("/status")
async def capture_status(_token=Depends(verify_panel_token)):
    await maintain_capture()
    settings = capture_settings()
    status = await asyncio.to_thread(get_capture_store().status)
    for name, policy in capture_policies(settings).items():
        status["policies"][name].update(policy)
    status["enabled"] = settings["request_capture_enabled"]
    status["directory"] = str(get_capture_store().root)
    status["availability"] = "retention_or_capacity_eviction_possible"
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


async def _query(policy, request_id, credential_name, since, before, limit, max_bytes):
    if policy not in POLICIES:
        raise HTTPException(400, "未知采集策略")
    if not math.isfinite(since) or before is not None and not math.isfinite(before):
        raise HTTPException(400, "时间戳必须是有限数值")
    rows = await asyncio.to_thread(
        get_capture_store().query, policy, capture_policies()[policy], request_id=request_id,
        credential_name=credential_name, since=since, before=before, limit=limit,
        max_result_bytes=max_bytes, with_status=True,
    )
    return rows


@router.get("/records")
async def capture_records(policy: str = "attempts", request_id: str | None = Query(None, max_length=256),
                          credential_name: str | None = Query(None, max_length=256),
                          since: float = 0, before: float | None = None,
                          limit: int = Query(100, ge=1, le=200), _token=Depends(verify_panel_token)):
    page = await _query(policy, request_id, credential_name, since, before, limit,
                        capture_settings()["request_capture_export_max_mb"] * 1024**2 // 2)
    rows = page["records"]
    return JSONResponse({"records": rows, "next_before": rows[-1]["created_at"] if rows else None,
                         "truncated": page["limited"],
                         "availability": "bounded_window; empty does not prove never captured"},
                        headers={"Cache-Control": "no-store"})


@router.get("/export")
async def capture_export(request_id: str | None = Query(None, max_length=256),
                         credential_name: str | None = Query(None, max_length=256),
                         since: float = 0, before: float | None = None,
                         limit: int = Query(100, ge=1, le=200), _token=Depends(verify_panel_token)):
    if not request_id and not credential_name:
        raise HTTPException(400, "导出时必须指定请求 ID 或凭证文件名")
    budget = capture_settings()["request_capture_export_max_mb"] * 1024**2
    result = {"version": 1, "request_id": request_id, "credential_name": credential_name,
              "truncated": False, "policies": {}, "next_before": {}}
    remaining = budget - 4096
    for name in POLICIES:
        page = await _query(name, request_id, credential_name, since, before, limit, max(remaining // 2, 0))
        rows = page["records"]
        result["policies"][name] = rows
        result["next_before"][name] = rows[-1]["created_at"] if rows and page["limited"] else None
        remaining -= len(json.dumps(rows, ensure_ascii=False).encode())
        if page["limited"] or remaining < budget // 4:
            result["truncated"] = True
    result["availability"] = "Original content may be disabled, omitted, expired or evicted; inspect each record."
    return JSONResponse(result, headers={"Cache-Control": "no-store",
                                         "Content-Disposition": 'attachment; filename="request-evidence.json"'})
