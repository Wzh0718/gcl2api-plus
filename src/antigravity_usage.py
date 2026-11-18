"""Rolling account admission limits based on upstream attempts and confirmed usage."""

import asyncio
from collections.abc import Mapping
from contextlib import closing
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3
import time

from log import log
from src.billing import calculate_usage_metrics, extract_usage_metadata


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
    def __init__(self, retry_after: int):
        super().__init__("Upstream account usage limit reached")
        self.retry_after = retry_after


def validate_usage_settings(value):
    if not isinstance(value, dict):
        raise ValueError("账号用量限制必须是对象")
    limits = {"enabled": False, **{name: 0 for name, _, _ in RULES}}
    if value.keys() - limits.keys():
        raise ValueError("存在未知的账号用量限制字段")
    limits.update(value)
    if type(limits["enabled"]) is not bool:
        raise ValueError("账号用量限制开关必须是布尔值")
    for name, _, _ in RULES:
        if type(limits[name]) is not int or not 0 <= limits[name] <= 2**53 - 1:
            raise ValueError(f"{name} 必须是非负安全整数")
    return limits


def _db_path() -> Path:
    return Path(os.getenv("CREDENTIALS_DIR", "./creds")) / "credentials.db"


@lru_cache(maxsize=8)
def _shared_settings(path: Path, second: int):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("SELECT value FROM config WHERE key = ?", (CONFIG_KEY,)).fetchone()
    return validate_usage_settings(json.loads(row[0]) if row else DEFAULT_USAGE_LIMITS)


async def get_usage_settings(*, for_admission: bool = False):
    from config import get_config_value

    if for_admission and int(os.getenv("WORKERS", "1")) > 1:
        return await asyncio.to_thread(_shared_settings, _db_path(), int(time.monotonic()))
    return validate_usage_settings(await get_config_value(CONFIG_KEY, DEFAULT_USAGE_LIMITS))


@lru_cache(maxsize=8)
def _initialize(path: Path) -> None:
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS account_usage (
                account TEXT NOT NULL,
                bucket INTEGER NOT NULL,
                requests INTEGER NOT NULL,
                generated_tokens INTEGER NOT NULL,
                PRIMARY KEY (account, bucket)
            ) WITHOUT ROWID
        """)
        db.execute("CREATE INDEX IF NOT EXISTS account_usage_bucket ON account_usage(bucket)")


def _add(db, account, bucket, requests, generated_tokens):
    db.execute("""
        INSERT INTO account_usage VALUES (?, ?, ?, ?)
        ON CONFLICT (account, bucket) DO UPDATE SET
            requests = requests + excluded.requests,
            generated_tokens = generated_tokens + excluded.generated_tokens
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
            totals[account] = {60: (minute_requests, minute_tokens), 3600: (hourly_requests, hourly_tokens)}
    return totals


def _accounts_exempted(db, accounts: list[str]) -> set[str]:
    exempted = set()
    for offset in range(0, len(accounts), 500):
        batch = accounts[offset:offset + 500]
        placeholders = ", ".join("?" for _ in batch)
        rows = db.execute(f"""
            SELECT filename FROM antigravity_credentials
            WHERE filename IN ({placeholders}) AND usage_limit_disabled = 1
        """, batch)
        exempted.update(filename for (filename,) in rows)
    return exempted


def _set_account_usage_mode(path: Path, account: str, *, exempt: bool) -> bool:
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        result = db.execute(
            "UPDATE antigravity_credentials SET usage_limit_disabled = ? WHERE filename = ?",
            (int(exempt), account),
        )
        return result.rowcount == 1


async def set_account_usage_mode(account: str, *, exempt: bool) -> bool:
    return await asyncio.to_thread(_set_account_usage_mode, _db_path(), account, exempt=exempt)


def _read_usage_state(path: Path, accounts: list[str], now: int, *, tracking: bool):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("BEGIN")
        exempted = _accounts_exempted(db, accounts)
        table_exists = tracking and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_usage'"
        ).fetchone()
        return exempted, _usage_totals(db, accounts, now) if table_exists else {}


async def get_account_usage_snapshots(accounts: list[str]) -> dict:
    if not accounts:
        return {}
    try:
        limits = await get_usage_settings(for_admission=True)
        configured = any(limits[name] for name, _, _ in RULES)
        tracking = limits["enabled"] and configured
        now = int(time.time())
        exempted, totals = await asyncio.to_thread(
            _read_usage_state, _db_path(), accounts, now, tracking=tracking,
        )
    except (sqlite3.Error, OSError, ValueError) as exc:
        log.warning(f"[ACCOUNT USAGE] Snapshot unavailable: {type(exc).__name__}")
        return {account: {"state": "unavailable"} for account in accounts}

    snapshots = {}
    for account in accounts:
        exempt = account in exempted
        mode = "off" if exempt else "inherit"
        if not tracking:
            state = "unconfigured" if limits["enabled"] else "disabled"
            snapshots[account] = {"state": state, "mode": mode}
            continue
        windows = []
        for seconds, period in WINDOWS.items():
            requests, tokens = totals.get(account, EMPTY_TOTALS)[seconds]
            windows.append({
                "seconds": seconds,
                "requests": {"used": requests, "limit": 0 if exempt else limits[f"requests_per_{period}"]},
                "generated_tokens": {"used": tokens, "limit": 0 if exempt else limits[f"generated_tokens_per_{period}"]},
            })
        snapshots[account] = {
            "state": "exempt" if exempt else "active", "mode": mode, "as_of": now, "windows": windows,
        }
    return snapshots


def _admit(path: Path, account: str, limits: dict) -> int:
    _initialize(path)
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        hour = now - 3600
        blocked = []
        if not _accounts_exempted(db, [account]):
            totals = _usage_totals(db, [account], now).get(account, EMPTY_TOTALS)
            blocked = [
                (seconds, column, totals[seconds][column - 1] - limits[name] + 1)
                for name, seconds, column in RULES
                if limits[name] and totals[seconds][column - 1] >= limits[name]
            ]
        if blocked:
            rows = list(db.execute("""
                SELECT bucket, requests, generated_tokens FROM account_usage
                WHERE account = ? AND bucket > ? ORDER BY bucket
            """, (account, hour)))
            retry_after = 1
            for seconds, column, excess in blocked:
                for row in rows:
                    if row[0] <= now - seconds:
                        continue
                    excess -= row[column]
                    if excess <= 0:
                        retry_after = max(retry_after, row[0] + seconds - now)
                        break
            raise AccountUsageLimitReached(retry_after)
        db.execute("DELETE FROM account_usage WHERE bucket <= ?", (hour,))
        _add(db, account, now, 1, 0)
    return now


def _undo_admission(path: Path, account: str, bucket: int) -> None:
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute(
            "UPDATE account_usage SET requests = requests - 1 WHERE account = ? AND bucket = ?",
            (account, bucket),
        )


def _confirm(path: Path, account: str, tokens: int) -> None:
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        _add(db, account, int(time.time()), 0, tokens)


class AccountUsageAttempt:
    def __init__(self, path: Path, account: str):
        self.path = path
        self.account = account
        self.confirmed = False

    async def confirm(self, usage_metadata: Mapping | None) -> None:
        if self.confirmed or not usage_metadata:
            return
        self.confirmed = True
        generation_fields = {
            key: value
            for key in ("candidatesTokenCount", "thoughtsTokenCount")
            if type(value := usage_metadata.get(key)) is int and 0 <= value < 2**53
        }
        usage = calculate_usage_metrics(generation_fields)
        tokens = usage.output_tokens + usage.thought_tokens
        if tokens:
            try:
                await asyncio.to_thread(_confirm, self.path, self.account, tokens)
            except sqlite3.Error as exc:
                self.confirmed = False
                log.error(f"[ACCOUNT USAGE] Failed to record confirmed tokens: {type(exc).__name__}")

    async def confirm_response(self, response) -> None:
        try:
            usage = extract_usage_metadata(response.json())
        except (ValueError, TypeError):
            return
        await self.confirm(usage)


async def begin_account_usage(account: str) -> AccountUsageAttempt | None:
    if not account:
        return None
    try:
        limits = await get_usage_settings(for_admission=True)
        if not limits["enabled"] or not any(limits[name] for name, _, _ in RULES):
            return None
        path = _db_path()
        reservation = asyncio.create_task(asyncio.to_thread(_admit, path, account, limits))
        try:
            await asyncio.shield(reservation)
        except asyncio.CancelledError:
            try:
                bucket = await reservation
                await asyncio.to_thread(_undo_admission, path, account, bucket)
            except AccountUsageLimitReached:
                pass
            except (sqlite3.Error, OSError) as exc:
                log.error(f"[ACCOUNT USAGE] Failed to cancel admission: {type(exc).__name__}")
            raise
    except (sqlite3.Error, OSError) as exc:
        log.error(f"[ACCOUNT USAGE] Admission store unavailable: {type(exc).__name__}")
        raise AccountUsageLimitReached(1) from exc
    return AccountUsageAttempt(path, account)
