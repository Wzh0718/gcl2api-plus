"""调用监控控制面板 API：账号 / API Key 聚合视图 + 实时逐条记录。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

import config
from log import log
from src.call_monitor import get_call_monitor
from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token


router = APIRouter(prefix="/call-monitor", tags=["call-monitor"])

SHANGHAI = ZoneInfo("Asia/Shanghai")

_RANGES = {"today", "7d", "30d", "all"}
_VIEWS = {"account", "key"}


def _range(value: str) -> str:
    if value not in _RANGES:
        raise HTTPException(status_code=400, detail="range 必须是 today、7d、30d 或 all")
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
    if range_name == "all":
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
    await monitor.preload()

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
    if degraded:
        buffer_rows = list(monitor._buffer)
        rows = _aggregate_from_buffer(
            buffer_rows,
            dims=dims,
            with_model=with_model,
            since_ts=since_ts,
            failed_only=failed_only,
        )
        counters = monitor.buffer_counters(since_ts=since_ts)
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
        if min_ts_getter is not None:
            oldest_ts = await min_ts_getter()

    range_secs = _range_seconds(range_name, since_ts, oldest_ts)
    return {
        "view": view,
        "range": range_name,
        "failed_only": failed_only,
        "degraded": degraded,
        "retention_days": await config.get_call_records_retention_days(),
        "counters": {
            "logs": counters.get("logs", 0),
            "failed": counters.get("failed", 0),
            "accounts": counters.get("accounts", 0),
            "keys": counters.get("keys", 0),
            "realtime": counters.get("logs", 0),
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
    await monitor.preload()

    paged = await monitor.realtime_page(
        page=page,
        page_size=page_size,
        since_ts=since_ts,
        failed_only=failed_only,
        api_key_id=api_key_id,
    )

    count_records = getattr(getattr(storage, "_backend", None), "count_call_records", None)
    if count_records is not None:
        counters = await count_records(since_ts=since_ts)
    else:
        counters = monitor.buffer_counters(since_ts=since_ts)

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
        },
        "records": paged["records"],
    }
