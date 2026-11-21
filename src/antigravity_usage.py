"""Rolling account admission limits based on upstream attempts and confirmed usage."""

import asyncio
from collections.abc import Mapping
from contextlib import closing, contextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import psycopg
from src.storage.main_postgres import main_dsn, sync_connection
from concurrent.futures import ThreadPoolExecutor

from src.storage.account_rotation import SQLITE_USAGE_SCHEMA
from src.storage.sqlite_pragmas import apply_sync_pragmas

# 账号用量表的写操作原本走 asyncio.to_thread，默认线程池只有 min(32, cpu+4)
# 个槽（4 核上是 8）。生产实测（2026-11-20）185 个在飞请求收尾时全部排在
# 这 8 个槽上，收尾阶段 p50 17.8s。这里换成专用线程池，避免与其它阻塞调用
# 抢默认执行器；SQLite 写锁本身仍然是串行的，但至少不在这里排队。
_DB_WORKERS = max(int(os.getenv("SQLITE_THREAD_WORKERS", "32") or 32), 4)
_DB_EXECUTOR: ThreadPoolExecutor | None = None


def _db_executor() -> ThreadPoolExecutor:
    global _DB_EXECUTOR
    if _DB_EXECUTOR is None:
        _DB_EXECUTOR = ThreadPoolExecutor(max_workers=_DB_WORKERS, thread_name_prefix="sqlite-w")
    return _DB_EXECUTOR


async def _in_db_thread(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_db_executor(), fn, *args)
import time

from log import log
from src.billing import calculate_usage_metrics, extract_usage_metadata
from src.rate_limits import RateLimit


CONFIG_KEY = "antigravity_account_usage"
DEFAULT_USAGE_LIMITS = {
    "requests_per_minute": 160,
    "requests_per_hour": 0,
    "generated_tokens_per_minute": 100_000,
    "generated_tokens_per_hour": 1_250_000,
}
WINDOWS = {60: "minute", 3600: "hour"}
EMPTY_TOTALS = {60: (0, 0), 3600: (0, 0)}
RULES = (
    ("requests_per_minute", 60, 1),
    ("requests_per_hour", 3600, 1),
    ("generated_tokens_per_minute", 60, 2),
    ("generated_tokens_per_hour", 3600, 2),
)


class AccountUsageLimitReached(Exception):
    def __init__(self, retry_after: int, *, rules: tuple[str, ...] = (), store_unavailable: bool = False):
        super().__init__("Gateway account usage check unavailable" if store_unavailable else "Gateway account usage limit reached")
        self.retry_after = retry_after
        self.rate_limit = RateLimit("gateway", "usage_store_unavailable" if store_unavailable else "account_usage_limit", rules)


def validate_usage_settings(value):
    if not isinstance(value, dict):
        raise ValueError("账号用量限制必须是对象")
    limits = {"enabled": False, **{name: 0 for name, _, _ in RULES}}
    if value.keys() - limits.keys():
        raise ValueError("存在未知的账号用量限制字段")
    limits.update(value)
    if type(limits["enabled"]) is not bool:
        raise ValueError("账号用量限制开关必须是布尔值")
    validate_account_usage_limits({name: limits[name] for name in DEFAULT_USAGE_LIMITS})
    return limits


def validate_account_usage_limits(limits):
    if not isinstance(limits, dict) or limits.keys() - DEFAULT_USAGE_LIMITS.keys():
        raise ValueError("存在未知的账号用量限制字段")
    for name, value in limits.items():
        if type(value) is not int or not 0 <= value <= 2**53 - 1:
            raise ValueError(f"{name} 必须是非负安全整数")
    return limits


def _db_path() -> Path:
    return Path(os.getenv("CREDENTIALS_DIR", "./creds")) / "credentials.db"


@contextmanager
def _connection(path, *, readonly=False):
    if main_dsn():
        with sync_connection() as db:
            yield db
    elif readonly:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            yield db
    else:
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            apply_sync_pragmas(db)
            yield db


@lru_cache(maxsize=8)
def _shared_settings(path: Path, second: int):
    with _connection(path, readonly=True) as db:
        row = db.execute("SELECT value FROM config WHERE key = ?", (CONFIG_KEY,)).fetchone()
    return validate_usage_settings(json.loads(row[0]) if row else DEFAULT_USAGE_LIMITS)


async def get_usage_settings(*, for_admission: bool = False):
    from config import get_config_value

    if for_admission and int(os.getenv("WORKERS", "1")) > 1:
        return await _in_db_thread(_shared_settings, _db_path(), int(time.monotonic()))
    return validate_usage_settings(await get_config_value(CONFIG_KEY, DEFAULT_USAGE_LIMITS))


@lru_cache(maxsize=8)
def _initialize(path: Path) -> None:
    with _connection(path) as db:
        for statement in SQLITE_USAGE_SCHEMA:
            db.execute(statement)



def _add(db, account, bucket, requests, generated_tokens):
    db.execute("""
        INSERT INTO account_usage VALUES (?, ?, ?, ?)
        ON CONFLICT (account, bucket) DO UPDATE SET
            requests = account_usage.requests + excluded.requests,
            generated_tokens = account_usage.generated_tokens + excluded.generated_tokens
    """, (account, bucket, requests, generated_tokens))


def _usage_totals(db, accounts: list[str], now: int):
    totals = {}
    for offset in range(0, len(accounts), 500):
        batch = accounts[offset:offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        rows = db.execute(f"""
            SELECT account,
                   SUM(CASE WHEN bucket > ? THEN requests ELSE 0 END),
                   SUM(CASE WHEN bucket > ? THEN generated_tokens ELSE 0 END),
                   SUM(requests), SUM(generated_tokens)
            FROM account_usage
            WHERE account IN ({placeholders}) AND bucket > ?
            GROUP BY account
        """, (now - 60, now - 60, *batch, now - 3600))
        for account, minute_requests, minute_tokens, hourly_requests, hourly_tokens in rows:
            # PostgreSQL SUM(bigint) returns Decimal. These counters are exact integers.
            totals[account] = {
                60: (int(minute_requests), int(minute_tokens)),
                3600: (int(hourly_requests), int(hourly_tokens)),
            }
    return totals


def _account_policies(db, accounts: list[str]) -> dict:
    policies = {}
    for offset in range(0, len(accounts), 500):
        batch = accounts[offset:offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        rows = db.execute(f"""
            SELECT filename, usage_limit_disabled, usage_limit_overrides
            FROM antigravity_credentials WHERE filename IN ({placeholders})
        """, batch)
        for filename, disabled, overrides in rows:
            policies[filename] = bool(disabled), validate_account_usage_limits(json.loads(overrides))
    return policies


def _should_track(global_limits, overrides):
    return any(overrides.values()) or (
        global_limits["enabled"] and any(global_limits[name] for name in DEFAULT_USAGE_LIMITS)
    )


def _effective_limits(global_limits, exempt, overrides):
    return {
        name: 0 if exempt else overrides.get(name, global_limits[name] if global_limits["enabled"] else 0)
        for name in DEFAULT_USAGE_LIMITS
    }


def _set_account_usage_policy(path: Path, account: str, mode: str, limits: dict | None) -> bool:
    overrides = None if mode == "off" else json.dumps(limits or {})
    with _connection(path) as db:
        result = db.execute(
            """UPDATE antigravity_credentials
               SET usage_resume_at = 0, usage_limit_disabled = ?, usage_limit_overrides = COALESCE(?, usage_limit_overrides)
               WHERE filename = ?""",
            (int(mode == "off"), overrides, account),
        )
        return result.rowcount == 1


async def set_account_usage_policy(account: str, *, mode: str, limits: dict | None) -> bool:
    return await _in_db_thread(_set_account_usage_policy, _db_path(), account, mode, limits)


def _read_usage_state(path: Path, accounts: list[str], now: int, global_limits: dict):
    with _connection(path, readonly=True) as db:
        if main_dsn():
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        else:
            db.execute("BEGIN")
        policies = _account_policies(db, accounts)
        tracked = [account for account in accounts if _should_track(global_limits, policies.get(account, (False, {}))[1])]
        table_exists = tracked and (main_dsn() or db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_usage'"
        ).fetchone())
        return policies, _usage_totals(db, tracked, now) if table_exists else {}


async def get_account_usage_snapshots(accounts: list[str], *, policy_details: bool = False) -> dict:
    if not accounts:
        return {}
    try:
        global_limits = await get_usage_settings(for_admission=True)
        now = int(time.time())
        policies, totals = await _in_db_thread(
            _read_usage_state, _db_path(), accounts, now, global_limits,
        )
    except (sqlite3.Error, psycopg.Error, OSError, ValueError) as exc:
        log.warning(f"[ACCOUNT USAGE] Snapshot unavailable: {type(exc).__name__}")
        return {account: {"state": "unavailable"} for account in accounts}

    snapshots = {}
    for account in accounts:
        if policy_details and account not in policies:
            continue
        exempt, overrides = policies.get(account, (False, {}))
        mode = "off" if exempt else ("custom" if overrides else "inherit")
        snapshot = snapshots[account] = {"mode": mode}
        if policy_details:
            snapshot.update(overrides=overrides, global_limits=global_limits)
        if not _should_track(global_limits, overrides):
            snapshot["state"] = "unconfigured" if global_limits["enabled"] or mode == "custom" else "disabled"
            continue
        limits = _effective_limits(global_limits, exempt, overrides)
        windows = []
        for seconds, period in WINDOWS.items():
            requests, tokens = totals.get(account, EMPTY_TOTALS)[seconds]
            windows.append({
                "seconds": seconds,
                "requests": {"used": requests, "limit": limits[f"requests_per_{period}"]},
                "generated_tokens": {"used": tokens, "limit": limits[f"generated_tokens_per_{period}"]},
            })
        snapshot.update(state="exempt" if exempt else "active", as_of=now, windows=windows)
    return snapshots


def _admit(path: Path, account: str, global_limits: dict, reservation_id=None) -> tuple[int, bool] | None:
    global_tracking = _should_track(global_limits, {})
    if not global_tracking and not main_dsn() and not path.is_file():
        return None
    with _connection(path) as db:
        if not global_tracking and reservation_id is None:
            _, overrides = _account_policies(db, [account]).get(account, (False, {}))
            if not _should_track(global_limits, overrides):
                return None
        if main_dsn():
            db.execute("SELECT pg_advisory_xact_lock(72210441, hashtext(?))", (account,))
            db.execute("SELECT filename FROM antigravity_credentials WHERE filename=? FOR UPDATE", (account,))
        else:
            _initialize(path)
            db.execute("BEGIN IMMEDIATE")
        policies = _account_policies(db, [account])
        exempt, overrides = policies.get(account, (False, {}))
        tracked = _should_track(global_limits, overrides)
        if not tracked and (reservation_id is None or account not in policies):
            return None
        limits = _effective_limits(global_limits, exempt, overrides)
        now = int(time.time())
        hour = now - 3600
        blocked = []
        if any(limits.values()):
            row = db.execute(
                "SELECT usage_resume_at FROM antigravity_credentials WHERE filename = ?", (account,)
            ).fetchone()
            if row and row[0] > now:
                raise AccountUsageLimitReached(row[0] - now)
            totals = _usage_totals(db, [account], now).get(account, EMPTY_TOTALS)
            blocked = [
                (name, seconds, column, totals[seconds][column - 1] - limits[name] + 1)
                for name, seconds, column in RULES
                if limits[name] and totals[seconds][column - 1] >= limits[name]
            ]
        if blocked:
            rows = list(db.execute("""
                SELECT bucket, requests, generated_tokens FROM account_usage
                WHERE account = ? AND bucket > ? ORDER BY bucket
            """, (account, hour)))
            retry_after = 1
            for _, seconds, column, excess in blocked:
                for row in rows:
                    if row[0] <= now - seconds:
                        continue
                    excess -= row[column]
                    if excess <= 0:
                        retry_after = max(retry_after, row[0] + seconds - now)
                        break
            db.execute("UPDATE antigravity_credentials SET usage_resume_at = ? WHERE filename = ?",
                       (now + retry_after, account))
            db.commit()  # Persist isolation before the exception exits the transaction.
            raise AccountUsageLimitReached(retry_after, rules=tuple(name for name, *_ in blocked))
        if main_dsn():
            db.execute("""DELETE FROM account_usage WHERE (account, bucket) IN (
                SELECT account, bucket FROM account_usage WHERE bucket <= ?
                ORDER BY bucket, account LIMIT 512 FOR UPDATE SKIP LOCKED
            )""", (hour,))
        else:
            db.execute("DELETE FROM account_usage WHERE bucket <= ?", (hour,))
        if reservation_id:
            db.execute("DELETE FROM routing_pending WHERE started <= ?", (hour,))
            db.execute("INSERT INTO routing_pending(id,account,started) VALUES(?,?,?)",
                       (reservation_id, account, now))
        if tracked:
            _add(db, account, now, 1, 0)
    return now, tracked


def _undo_admission(path, account, admission, reservation_id):
    bucket, tracked = admission
    with _connection(path) as db:
        if reservation_id:
            db.execute("DELETE FROM routing_pending WHERE id=?", (reservation_id,))
        if not tracked:
            return
        db.execute(
            "UPDATE account_usage SET requests = requests - 1 WHERE account = ? AND bucket = ?",
            (account, bucket),
        )


def _confirm(path: Path, account: str, tokens: int, reservation_id=None) -> None:
    with _connection(path) as db:
        if reservation_id:
            db.execute("DELETE FROM routing_pending WHERE id=?", (reservation_id,))
        if tokens:
            _add(db, account, int(time.time()), 0, tokens)


class AccountUsageAttempt:
    def __init__(self, path: Path, account: str, reservation_id=None):
        self.reservation_id = reservation_id
        self.path = path
        self.account = account
        self.confirmed = False
        self.tokens = 0

    async def confirm(self, usage_metadata: Mapping | None) -> None:
        if self.confirmed:
            return
        self.confirmed = True
        usage_metadata = usage_metadata or {}
        generation_fields = {
            key: value
            for key in ("candidatesTokenCount", "thoughtsTokenCount")
            if type(value := usage_metadata.get(key)) is int and 0 <= value < 2**53
        }
        usage = calculate_usage_metrics(generation_fields)
        self.tokens = max(self.tokens, usage.output_tokens + usage.thought_tokens)
        if self.tokens or self.reservation_id:
            try:
                await asyncio.shield(_in_db_thread(_confirm, self.path, self.account, self.tokens, self.reservation_id))
            except (sqlite3.Error, psycopg.Error) as exc:
                self.confirmed = False
                log.error(f"[ACCOUNT USAGE] Failed to record confirmed tokens: {type(exc).__name__}")

    async def confirm_response(self, response) -> None:
        try:
            usage = extract_usage_metadata(response.json())
        except (ValueError, TypeError):
            return
        await self.confirm(usage)


async def begin_account_usage(account: str, *, reserve_routing: bool = False) -> AccountUsageAttempt | None:
    if not account:
        return None
    try:
        limits = await get_usage_settings(for_admission=True)
        path = _db_path()
        reservation_id = uuid4().hex if reserve_routing else None
        reservation = asyncio.create_task(_in_db_thread(_admit, path, account, limits, reservation_id))
        try:
            bucket = await asyncio.shield(reservation)
        except asyncio.CancelledError:
            try:
                bucket = await reservation
                if bucket is not None:
                    await _in_db_thread(_undo_admission, path, account, bucket, reservation_id)
            except AccountUsageLimitReached:
                pass
            except (sqlite3.Error, psycopg.Error, OSError, ValueError) as exc:
                log.error(f"[ACCOUNT USAGE] Failed to cancel admission: {type(exc).__name__}")
            raise
    except (sqlite3.Error, psycopg.Error, OSError, ValueError) as exc:
        log.error(f"[ACCOUNT USAGE] Admission store unavailable: {type(exc).__name__}")
        raise AccountUsageLimitReached(1, store_unavailable=True) from exc
    return AccountUsageAttempt(path, account, reservation_id) if bucket is not None else None
