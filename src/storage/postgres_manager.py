"""Main-store PostgreSQL backend. Business rules live in the shared repositories."""

import asyncio
import json
import math
from contextlib import AsyncExitStack
from contextvars import Context
from datetime import datetime
import os
import time
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from log import log
from src.api_key_schedule import ApiKeySchedule

from src.rate_limits import selection_retry_at
from src.storage.account_cooldowns import AccountCooldowns, CachedRejection
from src.storage.main_postgres import (PostgresDatabase, close_sync_pool, connection_options,
                                       main_dsn, open_sync_pool, pg_query, safe_database_errors)
from src.storage import recent_monitor_cache
from src.storage.monitor_history import MonitorHistory, install as install_monitor_history
from src.storage.pg_model_stats import PostgresModelStats
from src.storage.refresh_import_queue import RefreshImportQueue
from src.request_timing import add_phase_ms, measure_pg, measure_phase
from src.storage.sqlite_manager import ROUTING_WINDOW_SECONDS, SQLiteManager

_MAX_CLAIMS_PER_BATCH = 64

DDL = Path(__file__).resolve().parents[2] / "scripts" / "postgres"


class PostgresManager(SQLiteManager):
    main_postgres = True

    async def initialize(self):
        async with self._lock:
            if self._initialized:
                return
            self._credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
            self._db_path = database = PostgresDatabase(main_dsn())
            self.refresh_import_queue = RefreshImportQueue(database, self._credentials_dir)
            claim_connections = int(os.getenv("MAIN_POSTGRES_CLAIM_CONNECTIONS", "4"))
            if not 1 <= claim_connections <= 16:
                raise ValueError("MAIN_POSTGRES_CLAIM_CONNECTIONS must be between 1 and 16")
            self._claim_batches = {}
            self._claim_tasks = set()
            self._cooldown_refresh = None
            self._next_cooldown_refresh = 0
            self._claim_minute = int(time.time()) // 60
            self._claim_counts = {"claim_batches": 0, "snapshot_reads": 0, "cache_hits": 0}
            self._claim_slots = asyncio.Semaphore(claim_connections)
            self._claim_pool = AsyncConnectionPool(
                main_dsn(), min_size=0, max_size=claim_connections, open=False,
                kwargs={**connection_options(database.schema), "autocommit": True},
            )
            await database.pool.open()
            try:
                await self._claim_pool.open()
                async with database.pool.connection() as conn:
                    await conn.execute("SELECT pg_advisory_xact_lock(72210438)")
                    schema = sql.Identifier(database.schema)
                    exists = await (await conn.execute("SELECT current_schema()")).fetchone()
                    if exists[0] != database.schema:
                        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
                    found = await (await conn.execute("SELECT to_regclass('api_keys')")).fetchone()
                    if found[0] is None:
                        for file in ("main_tables.sql", "main_indexes.sql"):
                            await conn.execute((DDL / file).read_text())
                    tables = re.findall(
                        r"^CREATE TABLE (\w+)", (DDL / "main_tables.sql").read_text(), re.M
                    )
                    # 老库不会重跑 main_tables.sql，新增的独立表写在这里幂等补齐；
                    # 它只含 CREATE ... IF NOT EXISTS，不引用任何基表，因此半成品
                    # schema 仍然由下面的完整性校验 fail closed。
                    await conn.execute((DDL / "main_upgrade.sql").read_text())
                    present = await (
                        await conn.execute(
                            "SELECT tablename FROM pg_tables WHERE schemaname=%s",
                            (database.schema,),
                        )
                    ).fetchall()
                    if missing := set(tables) - {row[0] for row in present}:
                        raise RuntimeError(
                            "Incomplete PostgreSQL main schema: " + ", ".join(sorted(missing))
                        )
                    await conn.execute((DDL / "main_runtime.sql").read_text())
                    await install_monitor_history(conn, postgres=True)
                    cache_id = await recent_monitor_cache.install(conn, True)
                    await conn.execute(
                        """
                        INSERT INTO api_keys(id,name,key_prefix,kind,status,currency,quota_period_start)
                        VALUES ('env','环境配置密钥','env','environment','active',%s,%s)
                        ON CONFLICT(id) DO NOTHING
                    """,
                        (
                            os.getenv("BILLING_CURRENCY", "CNY"),
                            datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                        ),
                    )
                await self._load_config_cache()
                if not self._config_loaded:
                    raise RuntimeError("PostgreSQL configuration load failed")
                stats_dsn = os.getenv("MODEL_STATS_POSTGRES_DSN")
                if stats_dsn:
                    self._pg_stats = await PostgresModelStats.open(stats_dsn)
                self._success_reader = database
                self._sync_key = (main_dsn(), database.schema)
                open_sync_pool(self._sync_key)
                self._cooldowns = AccountCooldowns(main_dsn() + "|" + database.schema)
                self.recent_cache = recent_monitor_cache.RecentMonitorCache(database, cache_id, self.monitor_excluded_accounts())
                self.monitor_history = MonitorHistory(database)
                await self._complete_ready_warmups()
                self._initialized = True
                log.info(f"PostgreSQL main pool ready: max_size={database.pool.max_size} per worker")
            except BaseException:
                await self._claim_pool.close()
                await database.pool.close()
                raise

    async def close(self):
        if not self._initialized:
            return
        if self._claim_tasks:
            await asyncio.gather(*self._claim_tasks)
        if self._cooldown_refresh is not None:
            await self._cooldown_refresh
        self._success_reader = None
        try:
            await super().close()
        finally:
            await self._cooldowns.close()
            close_sync_pool(self._sync_key)
            await self._claim_pool.close()
            await self._db_path.pool.close()

    async def _insert_monitor_batch(self, db, params, *, capture=False):
        # A single INSERT lets the AFTER STATEMENT trigger fold a whole batch before
        # taking summary row locks. Arrays also avoid the bind-count limit of VALUES.
        types = (
            "text", "float8", "text", "text", "text", "text", "text", "text", "text",
            "bigint", "bigint", "float8", "bigint", "bigint", "bigint", "bigint",
            "bigint", "text", "text", "text", "bytea", "text",
            "float8", "float8", "float8", "float8", "float8", "bytea",
        )
        columns = [list(values) for values in zip(*params, strict=True)]
        for index, kind in enumerate(types):
            if kind == "bytea":
                columns[index] = [v.encode() if isinstance(v, str) else v for v in columns[index]]
        target = self._CALL_RECORD_INSERT_SQL.split(" VALUES ", 1)[0]
        arrays = ", ".join(f"?::{kind}[]" for kind in types)
        query = f"{target} SELECT * FROM unnest({arrays})"
        if capture:
            cursor = await db.execute(f"{query} RETURNING {recent_monitor_cache.FIELDS}", columns)
            # RETURNING order need not match input or ID order.
            rows = await cursor.fetchall()
            return sorted(rows, key=lambda row: row[0])[-recent_monitor_cache.LIMIT:]
        await db.execute(query, columns)

    async def get_api_key_by_prefix(self, key_prefix):
        fields = tuple(name for name in self.API_KEY_COLUMNS if name != "secret_ciphertext")
        row = await self._db_path.single_row(
            f"SELECT {', '.join(fields)} FROM api_keys WHERE key_prefix=%s AND kind='managed'",
            (key_prefix,), operation="get_api_key_by_prefix",
        )
        return dict(zip(fields, row)) if row else None

    async def admit_api_key_request(self, api_key_id, now=None):
        timestamp = time.time() if now is None else now
        period, start = ApiKeySchedule().period(timestamp)
        (reason,) = await self._db_path.single_row(
            "SELECT gcli_admit_key(%s, %s, %s, %s)",
            (api_key_id, timestamp, period == "day", start), operation="admit_api_key_request",
        )
        return reason

    async def _claim_next(self, model_name, attempted, credential_name, health_policy=None):
        from config import get_antigravity_dormant_enabled_sync

        if not get_antigravity_dormant_enabled_sync():
            return await self._claim_active(model_name, attempted, credential_name, health_policy)
        # The Python quiet-hours decision must precede advancing a dormant account.
        from src.antigravity_pacing import is_dormant

        async with AsyncExitStack() as stack:
            with measure_phase("selection_lock_wait"):
                # Queue in this worker before consuming a shared DB connection.
                with measure_pg("local_queue"):
                    await stack.enter_async_context(self._selection_lock)
                db = await stack.enter_async_context(self._db_path.connect("claim_dormant"))
                await db.execute("SELECT pg_advisory_xact_lock(72210440)")
            with measure_phase("selection_locked_work"):
                now = time.time()
                where, params = self._claim_filter(model_name, attempted, credential_name, now, health_policy=health_policy)
                query = self._SELECT_CREDENTIAL.format(where_sql=where) + " FOR UPDATE"
                async with db.execute(query, params) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    async with db.execute(
                        self._RESUME_CREDENTIAL.format(where_sql=where), params
                    ) as cursor:
                        (resume,) = await cursor.fetchone()
                    return None, resume or 0, False
                if is_dormant(row[0]):
                    return row, 0, True
                cutoff = now - ROUTING_WINDOW_SECONDS
                await db.execute(self._ADVANCE_TURN, (cutoff, cutoff, now, row[0]))
                await db.commit()
                return row, 0, False

    def _count_claim_work(self, kind, count=1):
        minute = int(time.time()) // 60
        if minute != self._claim_minute:
            totals = " ".join(f"{name}={n}" for name, n in self._claim_counts.items())
            log.info(f"[CLAIM_COUNTS] minute={self._claim_minute} pid={os.getpid()} {totals}")
            self._claim_counts = dict.fromkeys(self._claim_counts, 0)
            self._claim_minute = minute
        self._claim_counts[kind] += count

    async def _read_cooldown_rows(self):
        # The same eligibility predicates as a claim, before applying account and model deadlines.
        # Keep all models so a cached miss on one can never suppress another.
        async with self._db_path.connect("account_cooldown_snapshot") as db:
            started = time.time()
            where, params = self._claim_filter(None, set(), None, started, include_held=True)
            records = await db.execute_fetchall(
                "SELECT filename, usage_resume_at, model_cooldowns, transient_hold_until "
                f"FROM antigravity_credentials WHERE {where}", params[:-1],
            )
        self._count_claim_work("snapshot_reads")
        rows = []
        for name, usage, models, hold in records:
            cooldowns = json.loads(models or "{}")
            rows.append((name, usage or 0, hold or 0, cooldowns if isinstance(cooldowns, dict) else {}))
        return started, rows

    async def _refresh_cooldowns(self):
        try:
            started, snapshot = await self._read_cooldown_rows()
            await self._cooldowns.publish(started, snapshot)
        except Exception as exc:
            log.warning(f"Account cooldown snapshot failed: {type(exc).__name__}")

    async def _claim_active(self, model_name, attempted, credential_name, health_policy=None):
        # Coalesce only live requests with identical eligibility. A free connection
        # starts immediately; no batching timer and no preallocated credentials.
        key = (model_name, frozenset(attempted), credential_name, health_policy)
        waiters = self._claim_batches.get(key)
        if waiters is None:
            waiters = self._claim_batches[key] = []
            task = asyncio.create_task(self._run_claim_batch(key, waiters), context=Context())
            self._claim_tasks.add(task)
            task.add_done_callback(self._claim_tasks.discard)
        future = asyncio.get_running_loop().create_future()
        waiters.append(future)
        if len(waiters) == _MAX_CLAIMS_PER_BATCH:
            del self._claim_batches[key]
        entered = time.monotonic()
        outcome = await future
        if isinstance(outcome, CachedRejection):
            selection_retry_at.set(outcome.retry_at)
            add_phase_ms("credential_cooldown_cache_hit", (time.monotonic() - entered) * 1000)
            return None, outcome.usage_resume_at, False
        result, retry_at, granted, connected, lock_ms, sql_ms, roundtrip = outcome
        selection_retry_at.set(retry_at)
        queued = max(0.0, granted - entered) * 1000
        pool_ms = (connected - granted) * 1000
        add_phase_ms("credential_pg_local_queue", queued)
        add_phase_ms("credential_pg_claim_pool", pool_ms)
        add_phase_ms("selection_lock_wait", queued + pool_ms + lock_ms)
        add_phase_ms("credential_pg_claim_roundtrip", roundtrip)
        add_phase_ms("credential_pg_advisory_server", lock_ms)
        add_phase_ms("credential_pg_claim_sql_server", sql_ms)
        add_phase_ms("selection_locked_work", sql_ms)
        return result

    async def _run_claim_batch(self, key, waiters):
        try:
            model, excluded, pinned, health_policy = key
            cache = self._cooldowns
            snapshot = await cache.read(model)
            if snapshot is not None:
                _, resume = cache.selection(snapshot[1], model, excluded, pinned, time.time())
                if resume is not None:
                    self._count_claim_work("cache_hits", sum(not f.done() for f in waiters))
                    for future in waiters:
                        if not future.done():
                            future.set_result(resume)
                    return
            async with self._claim_slots:
                granted = time.monotonic()
                async with self._claim_pool.connection() as conn:
                    connected = time.monotonic()
                    if self._claim_batches.get(key) is waiters:
                        del self._claim_batches[key]
                    active = [future for future in waiters if not future.cancelled()]
                    if not active:
                        return
                    blocked = set()
                    if snapshot is not None and snapshot[0] > time.time():
                        blocked, _ = cache.selection(snapshot[1], model, excluded, pinned, time.time())
                    rows, resume, retry_at, lock_ms, sql_ms = await self._rotate_batch(conn, key, len(active), blocked)
            self._count_claim_work("claim_batches")
            roundtrip = (time.monotonic() - connected) * 1000
            if (snapshot is None and time.monotonic() >= self._next_cooldown_refresh
                    and (self._cooldown_refresh is None or self._cooldown_refresh.done())):
                self._next_cooldown_refresh = time.monotonic() + cache.lifetime
                self._cooldown_refresh = asyncio.create_task(self._refresh_cooldowns(), context=Context())
            for index, future in enumerate(active):
                if not future.done():
                    row = rows[index % len(rows)] if rows else None
                    future.set_result(((row, resume, False), retry_at, granted, connected, lock_ms, sql_ms, roundtrip))
        except asyncio.CancelledError:
            for future in waiters:
                future.cancel()
            raise
        except Exception as exc:
            for future in waiters:
                if not future.done():
                    future.set_exception(exc)
        finally:
            if self._claim_batches.get(key) is waiters:
                del self._claim_batches[key]

    async def _rotate_batch(self, conn, key, count, blocked=()):
        model_name, attempted, credential_name, health_policy = key
        now = time.time()
        cutoff = now - ROUTING_WINDOW_SECONDS
        where, params = self._claim_filter(model_name, attempted, credential_name, now, health_policy=health_policy)
        # A cold Redis roster must give the same retry deadline as a cache hit.
        # Evaluate this aggregate only when the claim returns no account.
        eligible, eligible_params = self._claim_filter(None, attempted, credential_name, now, include_held=True, health_policy=health_policy)
        resume = f"""
            SELECT ARRAY[
                MIN(usage_resume_at) FILTER (
                    WHERE usage_resume_at > ? AND hold_until <= ? AND model_until <= ?
                ),
                MIN(GREATEST(usage_resume_at, hold_until, model_until))
            ] FROM (
                SELECT usage_resume_at, COALESCE(transient_hold_until, 0) AS hold_until,
                       COALESCE(gcli_cooldown(model_cooldowns, ?), 0) AS model_until
                FROM antigravity_credentials WHERE {eligible}
            ) AS deadlines"""
        path = "$." + json.dumps(model_name, ensure_ascii=False) if model_name else None
        resume_params = (now, now, now, path, *eligible_params[:-1])
        if blocked:
            where += " AND NOT (filename = ANY(?::text[]))"
            params = [*params[:-1], sorted(blocked), params[-1]]
        # The ordered candidates are one round. Position p receives turns
        # p, p + width, ... up to count, exactly as consecutive single claims.
        query = f"""
            WITH candidates AS MATERIALIZED (
                SELECT filename, routing_turn, rotation_order FROM antigravity_credentials
                WHERE {where} AND usage_resume_at <= ?
                ORDER BY {self._CREDENTIAL_ORDER} LIMIT ? FOR UPDATE
            ), ranked AS (
                SELECT filename, row_number() OVER (ORDER BY {self._CREDENTIAL_ORDER}) AS pos,
                       count(*) OVER () AS width FROM candidates
            ), turns AS (
                SELECT *, (?::bigint - pos) / width AS extra FROM ranked
            ), claimed AS (
                UPDATE antigravity_credentials a SET
                    call_count = coalesce(a.call_count, 0) + 1 + t.extra,
                    routing_turn = (SELECT coalesce(max(routing_turn), 0) FROM antigravity_credentials)
                                   + t.pos + t.extra * t.width,
                    routing_window_count = CASE WHEN coalesce(a.routing_window_started_at, 0) <= ?
                        THEN 0 ELSE coalesce(a.routing_window_count, 0) END + 1 + t.extra,
                    routing_window_started_at = CASE WHEN coalesce(a.routing_window_started_at, 0) <= ?
                        THEN ? ELSE a.routing_window_started_at END,
                    updated_at = unixepoch()
                FROM turns t WHERE a.filename = t.filename
                RETURNING a.filename, a.credential_data, a.model_cooldowns, a.enable_credit,
                          a.proxy_mode, a.proxy_url, a.proxy_group_id, a.bound_proxy_node_id, a.fixed_proxy_ip, t.pos
            )
            SELECT a.filename, a.credential_data, a.model_cooldowns, a.enable_credit,
                   a.proxy_mode, a.proxy_url, a.proxy_group_id, {self._NETWORK_COLUMNS},
                   to_jsonb(h), CASE WHEN a.filename IS NULL THEN ({resume}) END,
                   extract(epoch FROM clock_timestamp()-statement_timestamp())::float8*1000
            FROM (VALUES (1)) AS stub(i) LEFT JOIN claimed a ON true
            {self._NETWORK_JOINS}
            LEFT JOIN antigravity_account_health h ON h.credential_name = a.filename
            ORDER BY a.pos"""
        with safe_database_errors():
            async with conn.pipeline():
                async with conn.transaction():
                    lock = await conn.execute(
                        "SELECT extract(epoch FROM clock_timestamp()-statement_timestamp())::float8*1000 "
                        "FROM pg_advisory_xact_lock(72210440)"
                    )
                    result = await conn.execute(pg_query(query), (*params, count, count, cutoff, cutoff, now, *resume_params))
            (lock_ms,) = await lock.fetchone()
            records = await result.fetchall()
        rows = []
        for row in records:
            if row[0] is not None:
                network = self._resolved_network(self._network_from_row(row[7:-3]))
                rows.append((*row[:7], network, row[-3] or {}))
        usage_at, retry_at = records[0][-2] or (None, None)
        retry_at = retry_at if retry_at and retry_at > now and math.isfinite(retry_at) else 0
        sql_ms = max(row[-1] for row in records)
        return rows, int(usage_at or 0), retry_at, lock_ms, sql_ms

    @staticmethod
    def _money_e8_sql(column):
        return f"(CAST({column} AS numeric) * 100000000)"

    @staticmethod
    def _call_record_aggregation_source(since_ts, until_ts):
        return "call_records"

    async def record_success(self, filename, model_name=None, mode="geminicli"):
        self._ensure_initialized()
        name = os.path.basename(filename)
        try:
            if mode == "antigravity":
                (lock_ms,) = await self._db_path.single_row(
                    "SELECT gcli_record_antigravity_success(%s, %s)", (name, model_name or None),
                    operation="record_success",
                )
                add_phase_ms("success_lock_wait", lock_ms)
            elif mode == "geminicli":
                # GeminiCLI clears even an active cooldown for the successful model.
                await self._db_path.single_row("""
                    UPDATE credentials SET
                        last_success = CASE WHEN coalesce(error_codes,'') NOT IN ('','[]')
                            THEN floor(extract(epoch FROM statement_timestamp())) ELSE last_success END,
                        error_messages = CASE WHEN coalesce(error_codes,'') NOT IN ('','[]')
                            THEN '{}' ELSE error_messages END,
                        error_codes = CASE WHEN coalesce(error_codes,'') NOT IN ('','[]')
                            THEN '[]' ELSE error_codes END,
                        model_cooldowns = CASE WHEN model_cooldowns::jsonb ? %s
                            THEN (model_cooldowns::jsonb - %s::text)::text ELSE model_cooldowns END,
                        updated_at = floor(extract(epoch FROM statement_timestamp()))
                    WHERE filename = %s AND (coalesce(error_codes,'') NOT IN ('','[]')
                                             OR model_cooldowns::jsonb ? %s)
                    RETURNING filename""", (model_name or None, model_name or None, name, model_name or None),
                    operation="record_success",
                )
            else:
                raise ValueError(f"Invalid mode: {mode}")
        except Exception as exc:
            log.error(f"Error recording success for {name}: {type(exc).__name__}")

    async def set_model_cooldown(self, filename, model_name, cooldown_until, mode="geminicli"):
        self._ensure_initialized()
        try:
            table = self._get_table_name(mode)
            async with self._db_path.connect("set_model_cooldown") as db:
                if cooldown_until is None:
                    expression = "(coalesce(model_cooldowns, '{}')::jsonb - ?::text)::text"
                    args = [model_name]
                else:
                    expression = """jsonb_set(coalesce(model_cooldowns,'{}')::jsonb, ARRAY[?::text],
                        to_jsonb(CASE WHEN jsonb_typeof(model_cooldowns::jsonb -> ?::text) = 'number'
                                       AND (model_cooldowns::jsonb -> ?::text) > to_jsonb(unixepoch())
                                  THEN greatest((model_cooldowns::jsonb ->> ?::text)::double precision,
                                                 ?::double precision) ELSE ?::double precision END))::text"""
                    args = [model_name] * 4 + [cooldown_until, cooldown_until]
                result = await db.execute(
                    f"UPDATE {table} SET model_cooldowns={expression}, "
                    "updated_at=unixepoch() WHERE filename=?",
                    [*args, os.path.basename(filename)],
                )
                await db.commit()
                return result.rowcount == 1
        except (psycopg.Error, ValueError) as exc:
            log.error(f"Could not update model cooldown: {type(exc).__name__}")
            return False
