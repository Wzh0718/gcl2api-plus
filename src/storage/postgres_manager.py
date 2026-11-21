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

from log import log
from src.api_key_schedule import ApiKeySchedule

from src.storage.main_postgres import PostgresDatabase, close_sync_pool, main_dsn, open_sync_pool
from src.storage.pg_model_stats import PostgresModelStats
from src.storage.refresh_import_queue import RefreshImportQueue
from src.request_timing import measure_pg, measure_phase
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
            await database.pool.open()
            try:
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
            await self._db_path.pool.close()

    async def get_api_key_by_prefix(self, key_prefix):
        fields = tuple(name for name in self.API_KEY_COLUMNS if name != "secret_ciphertext")
        row = await self._db_path.single_row(
            f"SELECT {', '.join(fields)} FROM api_keys WHERE key_prefix=%s AND kind='managed'",
            (key_prefix,),
        )
        return dict(zip(fields, row)) if row else None

    async def admit_api_key_request(self, api_key_id, now=None):
        timestamp = time.time() if now is None else now
        period, start = ApiKeySchedule().period(timestamp)
        (reason,) = await self._db_path.single_row(
            "SELECT gcli_admit_key(%s, %s, %s, %s)",
            (api_key_id, timestamp, period == "day", start),
        )
        return reason

    async def _claim_next(self, model_name, attempted, credential_name):
        from src.antigravity_pacing import is_dormant

        async with AsyncExitStack() as stack:
            with measure_phase("selection_lock_wait"):
                # Queue in this worker before consuming a shared DB connection.
                with measure_pg("local_queue"):
                    await stack.enter_async_context(self._selection_lock)
                db = await stack.enter_async_context(self._db_path.connect())
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
            async with self._db_path.connect() as db:
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
