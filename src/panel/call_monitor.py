"""调用监控控制面板 API：账号 / API Key 聚合视图 + 实时逐条记录。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

import config
from log import log
from src.call_monitor import get_call_monitor
from src.storage.monitor_history import HistoryPreparing
from src.request_timing import get_inflight_requests
from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token


router = APIRouter(prefix="/call-monitor", tags=["call-monitor"])

SHANGHAI = ZoneInfo("Asia/Shanghai")

_RANGES = {"recent", "today", "7d", "30d", "all"}
_VIEWS = {"account", "key"}


def _history_preparing():
    return HTTPException(status_code=503, detail="正在构建历史汇总，完成后自动加载。",
                         headers={"Retry-After": "3", "X-Monitor-History": "building"})


def _range(value: str) -> str:
    if value not in _RANGES:
        raise HTTPException(status_code=400, detail="range 必须是 recent、today、7d、30d 或 all")
    return value


def _view(value: str) -> str:
    if value not in _VIEWS:
        raise HTTPException(status_code=400, detail="view 必须是 account 或 key")
    return value


def _since_ts(range_name: str) -> float:
    """范围起点：today 按北京时间零点，7d/30d 滚动窗口，all 不限。"""
    now = time.time()
    if range_name == "today":
        midnight = datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()
    if range_name == "7d":
        return now - 7 * 86400
    if range_name == "30d":
        return now - 30 * 86400
    return 0.0


def _range_seconds(range_name: str, since_ts: float, oldest_ts: Optional[float]) -> float:
    """TPS 口径的分母：所选范围覆盖的秒数。"""
    now = time.time()
    if range_name in {"recent", "all"}:
        return max(now - (oldest_ts or now), 1.0)
    return max(now - since_ts, 1.0)


def _enrich_rows(rows: List[Dict[str, Any]], range_secs: float) -> List[Dict[str, Any]]:
    for row in rows:
        row["tps"] = round(row.get("total", 0) / range_secs, 3)
    return rows


def _aggregate_from_buffer(
    buffer_rows: List[Dict[str, Any]],
    *,
    dims: Sequence[str],
    with_model: bool,
    since_ts: float,
    failed_only: bool,
) -> List[Dict[str, Any]]:
    """非 SQLite 后端兜底：基于内存缓冲在 Python 侧聚合（仅覆盖最近若干条）。

    dims 是分组维度，例如账号视图用 ("credential_name",)，密钥视图用
    ("api_key_id", "credential_name")。with_model=False 时不再按模型拆行，
    改为回填调用量最大的请求模型 / 上游物理模型。
    """
    grouped: Dict[Any, Dict[str, Any]] = {}
    model_counts: Dict[Any, Dict[Any, int]] = {}
    for row in buffer_rows:
        if row.get("created_at", 0.0) < since_ts:
            continue
        if failed_only and row.get("success"):
            continue
        if dims == ("credential_name",) and not row.get("credential_name"):
            continue
        dim_values = tuple(row.get(name) for name in dims)
        key = dim_values + ((row.get("model_name"),) if with_model else ())
        entry = grouped.get(key)
        if entry is None:
            entry = {name: value for name, value in zip(dims, dim_values)}
            entry.update({
                "group_key": dim_values[0],
                "total": 0, "success_count": 0, "failed_count": 0,
                "avg_gateway_seconds": 0.0, "_gw_sum": 0.0, "_gw_n": 0,
                "last_called_at": 0.0,
                "input_tokens": 0, "output_tokens": 0, "cache_tokens": 0,
                "thought_tokens": 0, "total_tokens": 0,
                "total_cost": 0.0, "currency": row.get("currency") or "CNY",
                "task_type": row.get("task_type") or "chat",
                "channel": row.get("channel") or "antigravity",
                "last_success": None, "last_status_code": None,
            })
            if with_model:
                entry["model_name"] = row.get("model_name")
            grouped[key] = entry
        entry["total"] += 1
        if row.get("success"):
            entry["success_count"] += 1
        else:
            entry["failed_count"] += 1
        gw = row.get("gateway_seconds")
        if isinstance(gw, (int, float)):
            entry["_gw_sum"] += float(gw)
            entry["_gw_n"] += 1
        for field in ("input_tokens", "output_tokens", "cache_tokens", "thought_tokens", "total_tokens"):
            entry[field] += int(row.get(field) or 0)
        try:
            entry["total_cost"] += float(row.get("total_cost") or 0)
        except (TypeError, ValueError):
            pass
        if row.get("created_at", 0.0) >= entry["last_called_at"]:
            entry["last_called_at"] = row.get("created_at", 0.0)
            entry["last_success"] = bool(row.get("success"))
            entry["last_status_code"] = row.get("status_code")
        if not with_model:
            bucket = model_counts.setdefault(key, {})
            model_key = (row.get("model_name") or "", row.get("upstream_model") or "")
            bucket[model_key] = bucket.get(model_key, 0) + 1

    result = []
    for key, entry in grouped.items():
        entry["avg_gateway_seconds"] = (
            round(entry["_gw_sum"] / entry["_gw_n"], 4) if entry["_gw_n"] else 0.0
        )
        entry["success_rate"] = (
            round(100.0 * entry["success_count"] / entry["total"], 1) if entry["total"] else None
        )
        entry["total_cost"] = f"{entry['total_cost']:.8f}"
        entry.pop("_gw_sum", None)
        entry.pop("_gw_n", None)
        if not with_model:
            top_model, top_model_calls = "", 0
            top_upstream, top_upstream_calls = "", 0
            for (model_name, upstream_model), calls in model_counts.get(key, {}).items():
                if model_name and calls > top_model_calls:
                    top_model, top_model_calls = model_name, calls
                if upstream_model and calls > top_upstream_calls:
                    top_upstream, top_upstream_calls = upstream_model, calls
            entry["top_model"] = top_model
            entry["top_model_calls"] = top_model_calls
            entry["top_upstream_model"] = top_upstream
            entry["top_upstream_calls"] = top_upstream_calls
        result.append(entry)
    result.sort(
        key=lambda item: (-item["total"],)
        + tuple(str(item.get(name) or "") for name in dims)
        + ((str(item.get("model_name") or ""),) if with_model else ())
    )
    return result


async def _attach_api_key_names(rows: list[Dict[str, Any]], backend: Any) -> None:
    ids = list(dict.fromkeys(row.get("api_key_id") or "env" for row in rows))
    batch_getter = getattr(backend, "get_call_monitor_key_names", None)
    names = {}
    if batch_getter is not None:
        names = await batch_getter(ids)
    else:
        getter = getattr(backend, "get_api_key_record", None)
        if getter:
            for key_id in ids:
                record = await getter(key_id)
                names[key_id] = (record or {}).get("name")
    for row in rows:
        key_id = row.get("api_key_id") or "env"
        row["api_key_name"] = names.get(key_id) or ("环境密钥" if key_id == "env" else "未命名密钥")


async def _attach_account_metadata(rows: list[Dict[str, Any]], backend: Any) -> None:
    getter = getattr(backend, "get_call_monitor_account_metadata", None)
    if getter is None:
        await _attach_account_emails(rows, backend)
        await _attach_warmup_status(rows, backend)
        return
    names = [row.get("credential_name", row.get("group_key")) for row in rows]
    accounts = await getter(list(dict.fromkeys(name for name in names if name)))
    for row, name in zip(rows, names):
        account = accounts.get(name, {})
        if row.get("channel") != "vertex":
            row["user_email"] = account.get("user_email", "")
        row["warmup_status"] = account.get("warmup_status", "off")
        row["warmup_level"] = account.get("warmup_level", 0)
        row["warmup_success_count"] = account.get("warmup_success_count", 0)
        row["warmup_max_level"] = account.get("warmup_max_level", 3)


async def _attach_account_emails(rows: list[Dict[str, Any]], backend: Any) -> None:
    getter = getattr(backend, "get_all_credential_states", None)
    if getter is None:
        return
    states_by_mode = {}
    for row in rows:
        mode = "vertex" if row.get("channel") == "vertex" else "antigravity"
        if mode == "vertex":
            continue
        if mode not in states_by_mode:
            states_by_mode[mode] = await getter(mode=mode)
        name = row.get("credential_name") or row.get("group_key")
        row["user_email"] = (states_by_mode[mode].get(name) or {}).get("user_email") or ""


async def _attach_warmup_status(rows: list[Dict[str, Any]], backend: Any) -> None:
    list_warmup = getattr(backend, "list_antigravity_warmup", None)
    if list_warmup is None:
        return
    accounts = (await list_warmup())["accounts"]
    for row in rows:
        name = row.get("credential_name") or row.get("group_key")
        state = accounts.get(name, {})
        config = state.get("config") or {}
        row["warmup_status"] = state.get("status") or "off"
        row["warmup_level"] = int(state.get("level") or 0)
        row["warmup_success_count"] = int(state.get("success_count") or 0)
        row["warmup_max_level"] = int(config.get("max_level") or 3)


@router.get("/overview")
async def call_monitor_overview(
    view: str = "account",
    range: str = "today",
    failed_only: bool = False,
    _token: str = Depends(verify_panel_token),
):
    """账号 / API Key 维度的聚合调用数据（含成功率、TPS、耗时、用量、花费）。

    account 视图按「账号 × 模型」聚合；key 视图按「密钥 × 账号」聚合（模型不再拆行），
    由前端折叠成「密钥 → 账号」两级表格。
    """
    view = _view(view)
    range_name = _range(range)
    since_ts = _since_ts(range_name)

    storage = await get_storage_adapter()
    monitor = await get_call_monitor(storage)

    backend = getattr(storage, "_backend", None)
    count_records = getattr(backend, "count_call_records", None)
    min_ts_getter = getattr(backend, "get_call_records_min_ts", None)
    aggregate = getattr(backend, "aggregate_call_records", None)
    aggregate_key_account = getattr(backend, "aggregate_key_account_usage", None)

    if view == "account":
        dims: Tuple[str, ...] = ("credential_name",)
        with_model = True
        aggregator = aggregate
    else:
        dims = ("api_key_id", "credential_name")
        with_model = False
        aggregator = aggregate_key_account

    degraded = aggregator is None or count_records is None
    oldest_ts: Optional[float] = None
    history = getattr(backend, "monitor_history", None)
    if history is not None and not degraded and range_name != "recent":
        try:
            rows, counters, oldest_ts = await history.overview(
                since_ts, view, failed_only, backend.monitor_visible_accounts()
            )
        except HistoryPreparing:
            raise _history_preparing()
    elif degraded or range_name == "recent":
        if degraded:
            await monitor.preload()
        if range_name == "recent":
            buffer_rows = await monitor.recent_snapshot()
        else:
            buffer_rows = [row for row in monitor._buffer if row["created_at"] >= since_ts]
        rows = _aggregate_from_buffer(
            buffer_rows,
            dims=dims,
            with_model=with_model,
            since_ts=since_ts,
            failed_only=failed_only,
        )
        counters = monitor.snapshot_counters(buffer_rows)
        if buffer_rows:
            oldest_ts = min(row.get("created_at", 0.0) for row in buffer_rows)
    else:
        if view == "account":
            rows = await aggregate(
                group_by="credential_name", since_ts=since_ts, failed_only=failed_only
            )
        else:
            rows = await aggregate_key_account(
                since_ts=since_ts, failed_only=failed_only
            )
        counters = await count_records(since_ts=since_ts, failed_only=False)
        if range_name == "all" and min_ts_getter is not None:
            oldest_ts = await min_ts_getter()

    if view == "key":
        await _attach_api_key_names(rows, backend)
    await _attach_account_metadata(rows, backend)
    range_secs = _range_seconds(range_name, since_ts, oldest_ts)
    return {
        "view": view,
        "range": range_name,
        "failed_only": failed_only,
        "degraded": degraded,
        "retention_days": await config.get_call_records_retention_days(),
        "counters": {
            "logs": counters.get("account_logs" if view == "account" else "logs", 0),
            "failed": counters.get("account_failed" if view == "account" else "failed", 0),
            "accounts": counters.get("accounts", 0),
            "keys": counters.get("keys", 0),
            "realtime": counters.get("logs", 0),
            "inflight": get_inflight_requests(),
        },
        "rows": _enrich_rows(rows, range_secs),
    }


@router.get("/realtime")
async def call_monitor_realtime(
    page: int = 1,
    page_size: int = 20,
    range: str = "today",
    failed_only: bool = False,
    api_key_id: Optional[str] = None,
    _token: str = Depends(verify_panel_token),
):
    """实时视图：逐条调用记录（时间倒序），服务端分页（SQLite 按页查询）。"""
    range_name = _range(range)
    since_ts = _since_ts(range_name)
    storage = await get_storage_adapter()
    monitor = await get_call_monitor(storage)

    filters = dict(page=page, page_size=page_size, failed_only=failed_only, api_key_id=api_key_id)
    if range_name == "recent":
        paged = await monitor.recent_page(**filters)
    elif getattr(storage._backend, "monitor_history", None) is not None:
        backend = storage._backend
        try:
            paged = await backend.monitor_history.page(
                backend, since_ts, page, page_size, failed_only, api_key_id,
                backend.monitor_visible_accounts(),
            )
        except HistoryPreparing:
            raise _history_preparing()
    else:
        await monitor.preload()
        paged = await monitor.realtime_page(since_ts=since_ts, **filters)

    counters = paged["counters"]

    getter = getattr(storage._backend, "list_telemetry_events", None)
    events = await getter([row.get("timing_id") for row in paged["records"]]) if getter else []
    for row in paged["records"]:
        matching = [event for event in events if event["timing_id"] == row.get("timing_id")
                    and event["filename"] == row.get("credential_name")]
        row["telemetry"] = {"count": len(matching), "outcomes": list(dict.fromkeys(event["outcome"] for event in matching))}
    await _attach_api_key_names(paged["records"], storage._backend)
    await _attach_account_metadata(paged["records"], storage._backend)
    total = int(paged["total"])
    page_size = int(paged["page_size"])
    total_pages = max(1, -(-total // page_size))
    return {
        "range": range_name,
        "failed_only": failed_only,
        "degraded": bool(paged["degraded"]),
        "retention_days": await config.get_call_records_retention_days(),
        "page": int(paged["page"]),
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "counters": {
            "logs": counters.get("logs", 0),
            "failed": counters.get("failed", 0),
            "accounts": counters.get("accounts", 0),
            "keys": counters.get("keys", 0),
            "realtime": counters.get("logs", 0),
            "inflight": get_inflight_requests(),
        },
        "records": paged["records"],
    }


@router.get("/telemetry")
async def call_monitor_telemetry(
    timing_id: str = Query(min_length=1, max_length=128),
    credential_name: str = Query(min_length=1, max_length=255),
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    events = await storage.list_telemetry_events([timing_id])
    return {"events": [event for event in events if event["filename"] == credential_name],
            "retention_days": 7, "max_events": 10000}


@router.get("/content")
async def call_monitor_content(
    request_id: Optional[str] = None,
    record_id: Optional[int] = None,
    _token: str = Depends(verify_panel_token),
):
    """Full content is loaded on demand behind panel authentication.

    面板列表里的行都带主键 ``id``，按 id 查详情无需额外索引；``request_id``
    只保留给历史链接兼容（无索引时是一次表扫描）。
    """
    if record_id is None and not request_id:
        raise HTTPException(status_code=422, detail="需要 record_id 或 request_id")
    storage = await get_storage_adapter()
    getter = getattr(getattr(storage, "_backend", None), "get_call_record_content", None)
    if getter is None:
        raise HTTPException(status_code=404, detail="当前存储不支持请求详情")
    content = await getter(request_id, record_id=record_id)
    if content is None:
        raise HTTPException(status_code=404, detail="记录不存在或已超过保留期")
    capture_id = content.get("request_features", {}).get("capture_id")
    if capture_id:
        import asyncio
        from src.capture_config import capture_policies
        from src.request_capture import get_capture_store
        from src.request_features import content_snapshot
        store = get_capture_store()
        policies = capture_policies()
        snapshots = []
        for name in ("normal", "errors"):
            snapshots.extend(await asyncio.to_thread(store.query, name, policies[name],
                                                     request_id=capture_id, limit=30, include_prior_success=False))
        snapshots.sort(key=lambda row: row.get("created_at", 0), reverse=True)
        chosen = next((r for r in snapshots if r.get("details", {}).get("upstream")), None)
        chosen = chosen or next((r for r in snapshots if r.get("details")), {})
        content["request_details"] = {stage: content_snapshot(payload) for stage, payload in
                                      chosen.get("details", {}).items() if isinstance(payload, dict)}
        feature_rows = await asyncio.to_thread(store.query, "features", policies["features"],
                                               request_id=capture_id, limit=30)
        captured_features = next((
            row["features"] for row in feature_rows
            if row.get("features") and (row.get("attempt_id") == chosen.get("attempt_id")
                                       if chosen else row["features"].get("upstream"))
        ), {})
        content["request_features"] = {**captured_features, **content["request_features"]}
        content.update(capture_id=capture_id, capture_state="available" if chosen else "not_available")
    from fastapi.responses import JSONResponse
    return JSONResponse(content, headers={"Cache-Control": "no-store"})
