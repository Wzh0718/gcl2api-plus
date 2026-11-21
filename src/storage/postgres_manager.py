"""Main-store PostgreSQL backend. Business rules live in the shared repositories."""

import asyncio
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

from src.storage.main_postgres import (PostgresDatabase, close_sync_pool, connection_options,
                                       main_dsn, open_sync_pool, pg_query, safe_database_errors)
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
                self._initialized = True
            except BaseException:
                await self._claim_pool.close()
                await database.pool.close()
                raise

    async def close(self):
        if not self._initialized:
            return
        if self._claim_tasks:
            await asyncio.gather(*self._claim_tasks)
        self._success_reader = None
        try:
            await super().close()
        finally:
            close_sync_pool(self._sync_key)
            await self._claim_pool.close()
            await self._db_path.pool.close()

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

    async def _claim_next(self, model_name, attempted, credential_name):
        from config import get_antigravity_dormant_enabled_sync

        if not get_antigravity_dormant_enabled_sync():
            return await self._claim_active(model_name, attempted, credential_name)
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
                where, params = self._claim_filter(model_name, attempted, credential_name, now)
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
                await db.execute(self._ADVANCE_TURN, (cutoff, now, cutoff, row[0]))
                await db.commit()
                return row, 0, False

    async def _claim_active(self, model_name, attempted, credential_name):
        # Coalesce only live requests with identical eligibility. A free connection
        # starts immediately; no batching timer and no preallocated credentials.
        key = (model_name, frozenset(attempted), credential_name)
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
        result, granted, connected, lock_ms, sql_ms, roundtrip = await future
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
            async with self._claim_slots:
                granted = time.monotonic()
                async with self._claim_pool.connection() as conn:
                    connected = time.monotonic()
                    if self._claim_batches.get(key) is waiters:
                        del self._claim_batches[key]
                    active = [future for future in waiters if not future.cancelled()]
                    if not active:
                        return
                    rows, resume, lock_ms, sql_ms = await self._rotate_batch(conn, key, len(active))
            roundtrip = (time.monotonic() - connected) * 1000
            for index, future in enumerate(active):
                if not future.done():
                    row = rows[index % len(rows)] if rows else None
                    future.set_result(((row, resume, False), granted, connected, lock_ms, sql_ms, roundtrip))
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

    async def _rotate_batch(self, conn, key, count):
        model_name, attempted, credential_name = key
        now = time.time()
        cutoff = now - ROUTING_WINDOW_SECONDS
        where, params = self._claim_filter(model_name, attempted, credential_name, now)
        resume = self._RESUME_CREDENTIAL.format(where_sql=where)
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
                          a.proxy_mode, a.proxy_url, a.proxy_group_id, t.pos
            )
            SELECT c.filename, c.credential_data, c.model_cooldowns, c.enable_credit,
                   c.proxy_mode, c.proxy_url, c.proxy_group_id,
                   CASE WHEN c.filename IS NULL THEN ({resume}) END,
                   extract(epoch FROM clock_timestamp()-statement_timestamp())::float8*1000
            FROM (VALUES (1)) AS stub(i) LEFT JOIN claimed c ON true ORDER BY c.pos"""
        with safe_database_errors():
            async with conn.pipeline():
                async with conn.transaction():
                    lock = await conn.execute(
                        "SELECT extract(epoch FROM clock_timestamp()-statement_timestamp())::float8*1000 "
                        "FROM pg_advisory_xact_lock(72210440)"
                    )
                    result = await conn.execute(pg_query(query), (*params, count, count, cutoff, cutoff, now, *params))
            (lock_ms,) = await lock.fetchone()
            records = await result.fetchall()
        rows = [row[:7] for row in records if row[0] is not None]
        resume_at = records[0][-2] or 0
        sql_ms = max(row[-1] for row in records)
        return rows, resume_at, lock_ms, sql_ms

    @staticmethod
    def _money_e8_sql(column):
        return f"(CAST({column} AS numeric) * 100000000)"

    @staticmethod
    def _call_record_aggregation_source(since_ts, until_ts):
        return "call_records"

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
