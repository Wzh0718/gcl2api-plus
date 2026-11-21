"""Main-store PostgreSQL backend. Business rules live in the shared repositories."""

from contextlib import AsyncExitStack
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
            self._claim_pool = AsyncConnectionPool(
                main_dsn(), min_size=0, max_size=1, open=False,
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
        # One connection per worker is reserved for the already-serialized rotation.
        # Each command gets a new READ COMMITTED snapshot after the advisory lock.
        async with AsyncExitStack() as stack:
            with measure_phase("selection_lock_wait"):
                # Waiters retain request cancellation semantics instead of expiring
                # as failed pool checkouts when a burst fills the rotation queue.
                with measure_pg("local_queue"):
                    await stack.enter_async_context(self._selection_lock)
                with measure_pg("claim_pool"):
                    conn = await stack.enter_async_context(self._claim_pool.connection())
            now = time.time()
            cutoff = now - ROUTING_WINDOW_SECONDS
            where, params = self._claim_filter(model_name, attempted, credential_name, now)
            candidate = self._SELECT_CREDENTIAL.format(where_sql=where) + " FOR UPDATE"
            resume = self._RESUME_CREDENTIAL.format(where_sql=where)
            advance = self._ADVANCE_TURN_SQL.format(target="(SELECT filename FROM candidate)")
            query = f"""
                WITH candidate AS ({candidate}), claimed AS (
                    {advance}
                    RETURNING filename, credential_data, model_cooldowns, enable_credit,
                              proxy_mode, proxy_url, proxy_group_id
                )
                SELECT c.*, CASE WHEN c.filename IS NULL THEN ({resume}) END,
                       extract(epoch FROM clock_timestamp()-statement_timestamp())::float8*1000
                FROM (VALUES (1)) AS stub(i) LEFT JOIN claimed c ON true"""
            with safe_database_errors(), measure_pg("claim_roundtrip"):
                async with conn.pipeline():
                    async with conn.transaction():
                        lock = await conn.execute(
                            "SELECT extract(epoch FROM clock_timestamp()-statement_timestamp())::float8*1000 "
                            "FROM pg_advisory_xact_lock(72210440)"
                        )
                        result = await conn.execute(pg_query(query), (*params, cutoff, now, cutoff, *params))
            (lock_ms,) = await lock.fetchone()
            row = await result.fetchone()
            add_phase_ms("credential_pg_advisory_server", lock_ms)
            add_phase_ms("selection_lock_wait", lock_ms)
            add_phase_ms("credential_pg_claim_sql_server", row[-1])
            add_phase_ms("selection_locked_work", row[-1])
            return (row[:7] if row[0] is not None else None), row[-2] or 0, False

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
