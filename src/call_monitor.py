"""逐请求调用监控：内存环形缓冲 + SQLite 持久化（滚动保留期）。

设计要点：

- 每个逻辑请求在计费去重通过后记录一次（由 ``BillingRecorder.record`` 触发），
  同时写入内存环形缓冲（实时视图秒开）和 SQLite ``call_records`` 表（重启不丢）。
- SQLite 为历史数据源：账号 / API Key 聚合视图按保留期窗口从库里统计；
  内存缓冲只服务「实时」视图的最新若干条。
- 保留期（默认 30 天，配置项 ``call_records_retention_days``，环境变量
  ``CALL_RECORDS_RETENTION_DAYS``）滚动清理：启动时执行一次，之后随写入节流执行。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any, Dict, List, Optional

from log import log

REALTIME_BUFFER_SIZE = 1000
CLEANUP_INTERVAL_SECONDS = 3600


def _buffer_size() -> int:
    try:
        return max(int(os.getenv("CALL_RECORDS_BUFFER_SIZE", str(REALTIME_BUFFER_SIZE))), 100)
    except ValueError:
        return REALTIME_BUFFER_SIZE


class CallMonitor:
    """逐请求调用记录的采集与读取入口。"""

    def __init__(self, storage: Any):
        self.storage = storage
        self._buffer: deque[Dict[str, Any]] = deque(maxlen=_buffer_size())
        self._lock = asyncio.Lock()
        self._last_cleanup = 0.0
        self._preloaded = False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _backend_method(self, name: str):
        backend = getattr(self.storage, "_backend", None)
        return getattr(backend, name, None) if backend is not None else None

    @staticmethod
    def _matches(
        row: Dict[str, Any],
        *,
        since_ts: float = 0.0,
        failed_only: bool = False,
        api_key_id: Optional[str] = None,
    ) -> bool:
        if row.get("created_at", 0.0) < since_ts:
            return False
        if failed_only and row.get("success"):
            return False
        if api_key_id and row.get("api_key_id") != api_key_id:
            return False
        return True

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def record(
        self,
        *,
        request_id: str,
        api_key_id: str = "env",
        credential_name: str = "",
        channel: str = "antigravity",
        model_name: str = "",
        task_type: str = "chat",
        success: bool,
        status_code: Optional[int] = None,
        gateway_seconds: Optional[float] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_tokens: int = 0,
        thought_tokens: int = 0,
        total_tokens: int = 0,
        total_cost: str = "0.00000000",
        currency: str = "CNY",
        created_at: Optional[float] = None,
    ) -> None:
        """记录一次调用。任何持久化失败都不影响请求主流程。"""
        row: Dict[str, Any] = {
            "request_id": request_id,
            "created_at": float(created_at if created_at is not None else time.time()),
            "api_key_id": api_key_id or "env",
            "credential_name": os.path.basename(credential_name or ""),
            "channel": channel or "antigravity",
            "model_name": model_name or "",
            "task_type": task_type or "chat",
            "success": bool(success),
            "status_code": status_code,
            "gateway_seconds": gateway_seconds,
            "input_tokens": max(int(input_tokens), 0),
            "output_tokens": max(int(output_tokens), 0),
            "cache_tokens": max(int(cache_tokens), 0),
            "thought_tokens": max(int(thought_tokens), 0),
            "total_tokens": max(int(total_tokens), 0),
            "total_cost": total_cost,
            "currency": currency or "CNY",
        }
        async with self._lock:
            self._buffer.append(row)
        try:
            insert = self._backend_method("insert_call_record")
            if insert is not None:
                await insert(**row)
        except Exception as exc:
            log.warning(f"[CALL MONITOR] 写入调用记录失败: {type(exc).__name__}")
        await self._maybe_cleanup()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def realtime(
        self,
        *,
        limit: int = 200,
        since_ts: float = 0.0,
        failed_only: bool = False,
        api_key_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """内存缓冲中的最新记录（时间倒序），供「实时」视图使用。"""
        rows = [
            row
            for row in reversed(self._buffer)
            if self._matches(row, since_ts=since_ts, failed_only=failed_only, api_key_id=api_key_id)
        ]
        return rows[: min(max(int(limit), 1), 1000)]

    def buffer_counters(self, *, since_ts: float = 0.0) -> Dict[str, int]:
        """基于内存缓冲的角标计数（实时视图口径）。"""
        rows = [row for row in self._buffer if row.get("created_at", 0.0) >= since_ts]
        return {
            "logs": len(rows),
            "failed": sum(1 for row in rows if not row.get("success")),
            "accounts": len({row.get("credential_name") for row in rows}),
            "keys": len({row.get("api_key_id") for row in rows}),
        }

    async def realtime_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        since_ts: float = 0.0,
        until_ts: Optional[float] = None,
        failed_only: bool = False,
        api_key_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """实时视图服务端分页：优先从 SQLite 按页查询（不受内存缓冲上限限制）。

        返回 {"records", "total", "page", "page_size", "degraded"}；
        total 为同筛选条件下的总条数（分页器用）。
        """
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 200)
        offset = (page - 1) * page_size
        list_fn = self._backend_method("list_call_records")
        count_fn = self._backend_method("count_call_records")
        if list_fn is not None and count_fn is not None:
            records = await list_fn(
                since_ts=since_ts,
                until_ts=until_ts,
                failed_only=failed_only,
                api_key_id=api_key_id,
                limit=page_size,
                offset=offset,
            )
            counters = await count_fn(
                since_ts=since_ts,
                until_ts=until_ts,
                failed_only=failed_only,
                api_key_id=api_key_id,
            )
            return {
                "records": records,
                "total": int(counters.get("logs", 0)),
                "page": page,
                "page_size": page_size,
                "degraded": False,
            }
        # 非 SQLite 后端降级：内存缓冲切片
        rows = [
            row
            for row in reversed(self._buffer)
            if self._matches(row, since_ts=since_ts, failed_only=failed_only, api_key_id=api_key_id)
        ]
        return {
            "records": rows[offset : offset + page_size],
            "total": len(rows),
            "page": page,
            "page_size": page_size,
            "degraded": True,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def preload(self) -> None:
        """启动时从 SQLite 预热内存缓冲，保证重启后实时视图有历史。"""
        if self._preloaded:
            return
        self._preloaded = True
        try:
            list_recent = self._backend_method("list_recent_call_records")
            if list_recent is None:
                return
            rows = await list_recent(limit=_buffer_size())
            async with self._lock:
                self._buffer.clear()
                self._buffer.extend(rows)
            if rows:
                log.info(f"[CALL MONITOR] 已预热 {len(rows)} 条历史调用记录")
        except Exception as exc:
            log.warning(f"[CALL MONITOR] 预热调用记录失败: {type(exc).__name__}")

    async def cleanup(self) -> int:
        """按保留期清理过期记录（SQLite + 内存缓冲），返回删除条数。"""
        from config import get_call_records_retention_days

        retention_days = await get_call_records_retention_days()
        cutoff = time.time() - retention_days * 86400
        deleted = 0
        try:
            delete_before = self._backend_method("delete_call_records_before")
            if delete_before is not None:
                deleted = await delete_before(cutoff)
        except Exception as exc:
            log.warning(f"[CALL MONITOR] 清理过期调用记录失败: {type(exc).__name__}")
        async with self._lock:
            kept = deque(
                (row for row in self._buffer if row.get("created_at", 0.0) >= cutoff),
                maxlen=self._buffer.maxlen,
            )
            self._buffer = kept
        self._last_cleanup = time.time()
        if deleted:
            log.info(
                f"[CALL MONITOR] 已清理 {deleted} 条超过 {retention_days} 天保留期的调用记录"
            )
        return deleted

    async def _maybe_cleanup(self) -> None:
        if time.time() - self._last_cleanup >= CLEANUP_INTERVAL_SECONDS:
            self._last_cleanup = time.time()  # 先占位，避免并发重复清理
            await self.cleanup()


_monitors: Dict[int, CallMonitor] = {}


async def get_call_monitor(storage: Any = None) -> CallMonitor:
    if storage is None:
        from src.storage_adapter import get_storage_adapter

        storage = await get_storage_adapter()
    key = id(storage)
    monitor = _monitors.get(key)
    if monitor is None:
        monitor = CallMonitor(storage)
        _monitors[key] = monitor
    return monitor
