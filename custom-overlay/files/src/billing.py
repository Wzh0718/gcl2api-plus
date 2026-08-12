"""SQLite-backed token billing with optional best-effort Redis mirroring.

The SQLite transaction is the source of truth.  Redis is deliberately kept out
of the request path when ``REDIS_URL`` is not configured and never gets an
in-memory retry queue.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from log import log
from src.redis_config import create_redis_client_from_env


MONEY_QUANTUM = Decimal("0.00000001")
SHANGHAI = ZoneInfo("Asia/Shanghai")


class BillingRedisUnavailable(RuntimeError):
    """Raised when the Redis-only dashboard cannot be read safely."""


@dataclass(frozen=True)
class UsageMetrics:
    input_tokens: int
    output_tokens: int
    cache_tokens: int
    thought_tokens: int
    total_tokens: int
    unknown_usage: bool = False


def _integer(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def calculate_usage_metrics(usage_metadata: Optional[Mapping[str, Any]]) -> UsageMetrics:
    """Convert Gemini usageMetadata into the billing dimensions."""
    if not isinstance(usage_metadata, Mapping) or not usage_metadata:
        return UsageMetrics(0, 0, 0, 0, 0, True)
    prompt_total = _integer(usage_metadata.get("promptTokenCount"))
    cache = _integer(usage_metadata.get("cachedContentTokenCount"))
    output = _integer(usage_metadata.get("candidatesTokenCount"))
    thought = _integer(usage_metadata.get("thoughtsTokenCount"))
    raw_total = usage_metadata.get("totalTokenCount")
    total = _integer(raw_total) if raw_total is not None else prompt_total + output + thought
    return UsageMetrics(max(prompt_total - cache, 0), output, cache, thought, total, False)


def calculate_costs(metrics: UsageMetrics, price: Mapping[str, Any]) -> Dict[str, Decimal]:
    """Calculate CNY/USD-style per-million-token prices with eight decimals."""
    def money(tokens: int, field: str) -> Decimal:
        rate = Decimal(str(price.get(field, "0") or "0"))
        return (Decimal(tokens) * rate / Decimal(1_000_000)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

    input_cost = money(metrics.input_tokens, "input_price")
    output_cost = money(metrics.output_tokens + metrics.thought_tokens, "output_price")
    cache_cost = money(metrics.cache_tokens, "cache_price")
    return {
        "input_cost": input_cost,
        "output_cost": output_cost,
        "cache_cost": cache_cost,
        "total_cost": (input_cost + output_cost + cache_cost).quantize(MONEY_QUANTUM),
    }


def extract_usage_metadata(payload: Any) -> Optional[Dict[str, Any]]:
    """Find the latest usageMetadata without retaining a response body."""
    if not isinstance(payload, Mapping):
        return None
    usage = payload.get("usageMetadata")
    if isinstance(usage, Mapping):
        return dict(usage)
    nested = payload.get("response")
    if isinstance(nested, Mapping):
        usage = nested.get("usageMetadata")
        if isinstance(usage, Mapping):
            return dict(usage)
    return None


class _RedisBillingMirror:
    def __init__(self, redis_client: Any = None):
        self.client = redis_client
        self._checked = redis_client is not None
        self._next_retry_at = 0.0

    async def _get_client(self):
        if self.client is not None:
            return self.client
        if self._checked and time.monotonic() < self._next_retry_at:
            return None
        self._checked = True
        try:
            self.client = create_redis_client_from_env(
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            if self.client is None:
                return None
            await self.client.ping()
            self._next_retry_at = 0.0
            return self.client
        except Exception as exc:
            await self._discard_client()
            self._next_retry_at = time.monotonic() + 30.0
            log.warning(f"[BILLING] Redis mirror unavailable: {type(exc).__name__}")
            return None

    async def _discard_client(self) -> None:
        """丢弃不可用的 Redis client 前显式关闭，避免连接池 fd 泄漏。"""
        client, self.client = self.client, None
        aclose = getattr(client, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception:
                pass

    @staticmethod
    def _key(date: str, api_key_id: str, credential: str, model: str, currency: str) -> str:
        prefix = os.getenv("BILLING_REDIS_PREFIX", "gcli:billing").strip(":")
        return f"{prefix}:v2:{date}:{currency}:{api_key_id}:{credential}:{model}"

    @staticmethod
    def _prefix() -> str:
        return os.getenv("BILLING_REDIS_PREFIX", "gcli:billing").strip(":")

    @classmethod
    def _ready_key(cls) -> str:
        return f"{cls._prefix()}:v2:ready"

    async def is_ready(self) -> bool:
        client = await self._get_client()
        if client is None:
            raise BillingRedisUnavailable("Redis 计费镜像不可用")
        if not hasattr(client, "get"):
            return True
        try:
            value = await client.get(self._ready_key())
            return self._text(value) == "1" if value is not None else False
        except Exception as exc:
            await self._discard_client()
            self._next_retry_at = time.monotonic() + 30.0
            log.warning(f"[BILLING] Redis readiness read failed: {type(exc).__name__}")
            raise BillingRedisUnavailable("Redis 计费镜像状态读取失败") from exc

    async def mark_ready(self) -> None:
        client = await self._get_client()
        if client is None or not hasattr(client, "set"):
            return
        try:
            await client.set(self._ready_key(), "1")
        except Exception as exc:
            await self._discard_client()
            self._next_retry_at = time.monotonic() + 30.0
            log.warning(f"[BILLING] Redis readiness write failed; SQLite retained: {type(exc).__name__}")

    async def mirror(self, *, billing_date: str, api_key_id: str, credential: str, model: str, currency: str,
                     metrics: UsageMetrics, costs: Mapping[str, Decimal], success: bool,
                     unknown_usage: bool) -> None:
        client = await self._get_client()
        if client is None:
            return
        key = self._key(billing_date, api_key_id, credential, model, currency)
        ttl = max(int(os.getenv("BILLING_REDIS_TTL_DAYS", "45")) * 86400, 1)
        try:
            values = {
                "input_tokens": metrics.input_tokens,
                "output_tokens": metrics.output_tokens,
                "cache_tokens": metrics.cache_tokens,
                "thought_tokens": metrics.thought_tokens,
                "total_tokens": metrics.total_tokens,
                "input_cost_e8": int(costs["input_cost"] * 100_000_000),
                "output_cost_e8": int(costs["output_cost"] * 100_000_000),
                "cache_cost_e8": int(costs["cache_cost"] * 100_000_000),
                "total_cost_e8": int(costs["total_cost"] * 100_000_000),
                "success_count": 1 if success else 0,
                "failed_count": 0 if success else 1,
                "unknown_usage_count": 1 if unknown_usage else 0,
            }
            if hasattr(client, "hincrby"):
                for field, value in values.items():
                    await client.hincrby(key, field, value)
            elif hasattr(client, "incrby"):
                for field, value in values.items():
                    await client.incrby(f"{key}:{field}", value)
            if hasattr(client, "hset"):
                await client.hset(key, mapping={
                    "billing_date": billing_date,
                    "api_key_id": api_key_id,
                    "credential_name": credential,
                    "model": model,
                    "currency": currency,
                })
            await client.expire(key, ttl)
        except Exception as exc:
            await self._discard_client()
            self._next_retry_at = time.monotonic() + 30.0
            log.warning(f"[BILLING] Redis mirror write failed; SQLite retained: {type(exc).__name__}")

    async def replace(self, *, billing_date: str, api_key_id: str, credential: str, model: str, currency: str,
                      metrics: UsageMetrics, costs: Mapping[str, Decimal], success_count: int,
                      failed_count: int, unknown_usage_count: int) -> None:
        client = await self._get_client()
        if client is None:
            return
        key = self._key(billing_date, api_key_id, credential, model, currency)
        ttl = max(int(os.getenv("BILLING_REDIS_TTL_DAYS", "45")) * 86400, 1)
        values = {
            "billing_date": billing_date, "api_key_id": api_key_id,
            "credential_name": credential,
            "model": model, "currency": currency,
            "input_tokens": metrics.input_tokens, "output_tokens": metrics.output_tokens,
            "cache_tokens": metrics.cache_tokens, "thought_tokens": metrics.thought_tokens,
            "total_tokens": metrics.total_tokens,
            "input_cost_e8": int(costs["input_cost"] * 100_000_000),
            "output_cost_e8": int(costs["output_cost"] * 100_000_000),
            "cache_cost_e8": int(costs["cache_cost"] * 100_000_000),
            "total_cost_e8": int(costs["total_cost"] * 100_000_000),
            "success_count": int(success_count), "failed_count": int(failed_count),
            "unknown_usage_count": int(unknown_usage_count),
        }
        try:
            await client.hset(key, mapping=values)
            await client.expire(key, ttl)
        except Exception as exc:
            await self._discard_client()
            self._next_retry_at = time.monotonic() + 30.0
            log.warning(f"[BILLING] Redis mirror rebuild failed; SQLite retained: {type(exc).__name__}")

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _range_dates(range_name: str) -> list[str]:
        days_by_range = {"today": 1, "7d": 7, "14d": 14, "30d": 30}
        if range_name not in days_by_range:
            raise ValueError("range must be today, 7d, 14d or 30d")
        today = datetime.now(SHANGHAI).date()
        return [(today - timedelta(days=offset)).isoformat() for offset in range(days_by_range[range_name])]

    async def _read_hash_batch(self, client: Any, keys: list[Any]) -> list[Mapping[str, Any]]:
        if not keys:
            return []
        if hasattr(client, "pipeline"):
            pipeline = client.pipeline(transaction=False)
            for key in keys:
                pipeline.hgetall(key)
            return await pipeline.execute()
        return await asyncio.gather(*(client.hgetall(key) for key in keys))

    async def dashboard(
        self, range_name: str, page_size: int = 10, api_key_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build one bounded dashboard response from Redis without touching SQLite."""
        client = await self._get_client()
        if client is None:
            raise BillingRedisUnavailable("Redis 计费镜像不可用")

        dates = self._range_dates(range_name)
        page_size = min(max(int(page_size), 1), 100)
        batch_size = min(max(int(os.getenv("BILLING_REDIS_SCAN_BATCH_SIZE", "200")), 1), 1000)
        max_keys = min(max(int(os.getenv("BILLING_REDIS_SCAN_MAX_KEYS", "100000")), 1), 1_000_000)
        numeric_fields = (
            "input_tokens", "output_tokens", "cache_tokens", "thought_tokens", "total_tokens",
            "input_cost_e8", "output_cost_e8", "cache_cost_e8", "total_cost_e8",
            "success_count", "failed_count", "unknown_usage_count",
        )
        totals = {field: 0 for field in numeric_fields}
        daily: Dict[str, Dict[str, int]] = {}
        accounts: Dict[tuple[str, str], Dict[str, int]] = {}
        models: Dict[tuple[str, str], Dict[str, int]] = {}
        keys: Dict[tuple[str, str], Dict[str, int]] = {}
        scanned_keys = 0

        async def aggregate(key: Any, raw: Mapping[str, Any]) -> None:
            nonlocal scanned_keys
            scanned_keys += 1
            if scanned_keys > max_keys:
                raise BillingRedisUnavailable(f"Redis 计费键超过安全上限 {max_keys}")
            row = {self._text(field): self._text(value) for field, value in raw.items()}
            key_text = self._text(key)
            billing_date = row.get("billing_date", "")
            row_api_key_id = row.get("api_key_id", "")
            credential = row.get("credential_name", "")
            model = row.get("model", "")
            currency = row.get("currency", "")
            if not all((billing_date, credential, model, currency)):
                for candidate_date in dates:
                    marker = f"{self._prefix()}:v2:{candidate_date}:"
                    if key_text.startswith(marker):
                        parts = key_text[len(marker):].split(":", 3)
                        if len(parts) == 4:
                            billing_date = billing_date or candidate_date
                            currency = currency or parts[0]
                            row_api_key_id = row_api_key_id or parts[1]
                            credential = credential or parts[2]
                            model = model or parts[3]
                        break
            if not all((billing_date, row_api_key_id, credential, model, currency)):
                return
            values = {field: _integer(row.get(field)) for field in numeric_fields}
            key_item = keys.setdefault((row_api_key_id, currency), {
                "total_tokens": 0, "total_cost_e8": 0, "success_count": 0,
                "failed_count": 0, "unknown_usage_count": 0,
            })
            for field in key_item:
                key_item[field] += values[field]
            if api_key_id and row_api_key_id != api_key_id:
                return
            for field, value in values.items():
                totals[field] += value
            day = daily.setdefault(billing_date, {"total_tokens": 0, "total_cost_e8": 0})
            day["total_tokens"] += values["total_tokens"]
            day["total_cost_e8"] += values["total_cost_e8"]
            for target, name in ((accounts, credential), (models, model)):
                item = target.setdefault((name, currency), {
                    "total_tokens": 0, "total_cost_e8": 0, "success_count": 0,
                    "failed_count": 0, "unknown_usage_count": 0,
                })
                for field in item:
                    item[field] += values[field]

        try:
            for billing_date in dates:
                batch: list[Any] = []
                async for key in client.scan_iter(match=f"{self._prefix()}:v2:{billing_date}:*", count=batch_size):
                    batch.append(key)
                    if len(batch) < batch_size:
                        continue
                    rows = await self._read_hash_batch(client, batch)
                    for batch_key, row in zip(batch, rows):
                        await aggregate(batch_key, row)
                    batch.clear()
                rows = await self._read_hash_batch(client, batch)
                for batch_key, row in zip(batch, rows):
                    await aggregate(batch_key, row)
        except BillingRedisUnavailable:
            raise
        except Exception as exc:
            await self._discard_client()
            self._next_retry_at = time.monotonic() + 30.0
            log.warning(f"[BILLING] Redis dashboard read failed: {type(exc).__name__}")
            raise BillingRedisUnavailable("Redis 计费镜像读取失败") from exc

        def money(value: int) -> str:
            return f"{Decimal(value) / Decimal(100_000_000):.8f}"

        def ranking(source: Dict[tuple[str, str], Dict[str, int]], name_key: str) -> Dict[str, Any]:
            ordered = sorted(source.items(), key=lambda item: (-item[1]["total_tokens"], item[0][0], item[0][1]))
            items = []
            for (name, currency), values in ordered[:page_size]:
                items.append({
                    name_key: name, "currency": currency,
                    "total_tokens": values["total_tokens"], "total_cost": money(values["total_cost_e8"]),
                    "success_count": values["success_count"], "failed_count": values["failed_count"],
                    "unknown_usage_count": values["unknown_usage_count"],
                })
            return {"items": items, "page": 1, "page_size": page_size, "total": len(ordered), "has_more": len(ordered) > page_size}

        summary = {
            "range": range_name, "timezone": "Asia/Shanghai", "dates": dates,
            "input_tokens": totals["input_tokens"], "output_tokens": totals["output_tokens"],
            "cache_tokens": totals["cache_tokens"], "thought_tokens": totals["thought_tokens"],
            "total_tokens": totals["total_tokens"], "input_cost": money(totals["input_cost_e8"]),
            "output_cost": money(totals["output_cost_e8"]), "cache_cost": money(totals["cache_cost_e8"]),
            "total_cost": money(totals["total_cost_e8"]), "success_count": totals["success_count"],
            "failed_count": totals["failed_count"], "unknown_usage_count": totals["unknown_usage_count"],
            "daily_trend": [
                {"billing_date": day, "total_tokens": daily[day]["total_tokens"], "total_cost": money(daily[day]["total_cost_e8"])}
                for day in sorted(daily)
            ],
        }
        return {
            "source": "redis", "scanned_keys": scanned_keys, "summary": summary,
            "accounts": ranking(accounts, "credential_name"), "models": ranking(models, "model"),
            "keys": ranking(keys, "api_key_id"),
        }


class BillingRecorder:
    """Record one logical Antigravity request at most once."""

    def __init__(self, storage: Any, redis_client: Any = None):
        self.storage = storage
        self.redis = _RedisBillingMirror(redis_client)
        self._prices: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._price_lock = asyncio.Lock()

    @property
    def currency(self) -> str:
        return os.getenv("BILLING_CURRENCY", "CNY") or "CNY"

    @property
    def dedupe_ttl_seconds(self) -> int:
        try:
            return max(int(os.getenv("BILLING_DEDUPE_TTL_DAYS", "7")), 1) * 86400
        except ValueError:
            return 7 * 86400

    async def _price_for(self, model: str) -> Dict[str, Any]:
        async with self._price_lock:
            if model in self._prices:
                self._prices.move_to_end(model)
                return self._prices[model]
            rows = await self.storage.list_billing_prices()
            selected = next((row for row in rows if row["model"] == model), None)
            if selected is None:
                selected = next((row for row in rows if row["model"] == "default"), None)
            selected = selected or {
                "model": "default", "input_price": "0", "output_price": "0", "cache_price": "0", "currency": self.currency,
            }
            self._prices[model] = selected
            while len(self._prices) > 128:
                self._prices.popitem(last=False)
            return selected

    async def invalidate_price_cache(self) -> None:
        async with self._price_lock:
            self._prices.clear()

    async def recalculate_costs(self, model: Optional[str] = None) -> int:
        """价格变更后重算历史汇总成本（后台任务入口）。

        model 为具体模型名时重算该模型；model 为 None 表示 default 价格变更，
        重算所有没有独立价格行的模型。完成后同步重建密钥配额与 Redis 镜像。
        """
        price = await self._price_for(model or "default")
        updated = 0
        page = 1
        page_size = 500
        while True:
            rows = await self.storage.list_billing_daily_for_reprice(model, page, page_size)
            if not rows:
                break
            for row in rows:
                metrics = UsageMetrics(
                    int(row["input_tokens"]), int(row["output_tokens"]), int(row["cache_tokens"]),
                    int(row["thought_tokens"]), int(row["total_tokens"]), False,
                )
                costs = calculate_costs(metrics, price)
                await self.storage.update_billing_daily_costs(
                    billing_date=row["billing_date"],
                    api_key_id=row["api_key_id"],
                    credential_name=row["credential_name"],
                    model=row["model"],
                    currency=row["currency"],
                    input_cost=f"{costs['input_cost']:.8f}",
                    output_cost=f"{costs['output_cost']:.8f}",
                    cache_cost=f"{costs['cache_cost']:.8f}",
                    total_cost=f"{costs['total_cost']:.8f}",
                )
                updated += 1
            if len(rows) < page_size:
                break
            page += 1
        await self.storage.rebuild_api_key_quotas()
        await self.rebuild_redis()
        return updated

    async def record(self, *, request_id: str, credential_name: str, model: str,
                    usage_metadata: Optional[Mapping[str, Any]] = None,
                    success: bool, timestamp: Optional[datetime] = None,
                    usage: Optional[UsageMetrics] = None,
                    api_key_id: str = "env") -> bool:
        if not request_id:
            return False
        metrics = usage or calculate_usage_metrics(usage_metadata)
        price = await self._price_for(model)
        costs = calculate_costs(metrics, price)
        now = timestamp.astimezone(SHANGHAI) if timestamp else datetime.now(SHANGHAI)
        billing_date = now.date().isoformat()
        currency = str(price.get("currency") or self.currency)
        first = await self.storage.record_billing_usage(
            request_id=request_id,
            api_key_id=api_key_id,
            billing_date=billing_date,
            credential_name=credential_name,
            model=model,
            currency=currency,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            cache_tokens=metrics.cache_tokens,
            thought_tokens=metrics.thought_tokens,
            total_tokens=metrics.total_tokens,
            input_cost=f"{costs['input_cost']:.8f}",
            output_cost=f"{costs['output_cost']:.8f}",
            cache_cost=f"{costs['cache_cost']:.8f}",
            total_cost=f"{costs['total_cost']:.8f}",
            success=success,
            unknown_usage=metrics.unknown_usage,
            dedupe_ttl_seconds=self.dedupe_ttl_seconds,
        )
        if first:
            was_connected = self.redis.client is not None
            client = await self.redis._get_client()
            if client is not None and not was_connected:
                await self.rebuild_redis()
            elif client is not None:
                await self.redis.mirror(
                    billing_date=billing_date, api_key_id=api_key_id,
                    credential=credential_name, model=model, currency=currency,
                    metrics=metrics, costs=costs, success=success, unknown_usage=metrics.unknown_usage,
                )
        return first

    async def rebuild_redis(self, days: int = 45) -> int:
        """Rebuild the bounded Redis mirror from SQLite after Redis recovery."""
        client = await self.redis._get_client()
        if client is None:
            return 0
        count = 0
        page = 1
        page_size = 500
        while True:
            rows = await self.storage.list_billing_daily(min(days, 45), page, page_size)
            if not rows:
                break
            for row in rows:
                metrics = UsageMetrics(
                    int(row["input_tokens"]), int(row["output_tokens"]), int(row["cache_tokens"]),
                    int(row["thought_tokens"]), int(row["total_tokens"]), False,
                )
                costs = {key: Decimal(str(row[value])) for key, value in {
                    "input_cost": "input_cost", "output_cost": "output_cost", "cache_cost": "cache_cost", "total_cost": "total_cost",
                }.items()}
                await self.redis.replace(
                    billing_date=row["billing_date"], api_key_id=row.get("api_key_id", "env"),
                    credential=row["credential_name"], model=row["model"], currency=row["currency"],
                    metrics=metrics, costs=costs, success_count=row["success_count"],
                    failed_count=row["failed_count"], unknown_usage_count=row["unknown_usage_count"],
                )
                count += 1
            if len(rows) < page_size:
                break
            page += 1
        await self.redis.mark_ready()
        return count

    async def get_redis_dashboard(
        self, range_name: str = "today", page_size: int = 10,
        api_key_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not await self.redis.is_ready():
            await self.rebuild_redis()
            if not await self.redis.is_ready():
                raise BillingRedisUnavailable("Redis 计费镜像尚未就绪")
        return await self.redis.dashboard(range_name, page_size, api_key_id=api_key_id)


_recorders: Dict[int, BillingRecorder] = {}


async def get_billing_recorder(storage: Any = None) -> BillingRecorder:
    if storage is None:
        from src.storage_adapter import get_storage_adapter
        storage = await get_storage_adapter()
    key = id(storage)
    recorder = _recorders.get(key)
    if recorder is None:
        recorder = _recorders[key] = BillingRecorder(storage)
    return recorder
