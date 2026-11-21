"""Pooled PostgreSQL connections for the shared main-store repositories."""

from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from functools import lru_cache
import os
import re

from threading import Lock

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from src.request_timing import measure_pg


def main_dsn():
    return os.getenv("MAIN_POSTGRES_DSN", "").strip()


def schema_name():
    schema = os.getenv("MAIN_POSTGRES_SCHEMA", "gcli_main")
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise ValueError("Invalid MAIN_POSTGRES_SCHEMA")
    return schema


def connection_options(schema):
    return {"options": f"-c search_path={schema} -c timezone=UTC", "connect_timeout": 5}


# Repository SQL is parameterized using DB-API qmark and named parameters.
# Preserve quoted literals and identifiers when translating parameter markers.
_tokens = re.compile(r"""'(?:''|[^'])*'|"(?:""|[^"])*"|::|:([A-Za-z_]\w*)|\?""")


@lru_cache(maxsize=1024)
def pg_query(query):
    query = query.replace(
        "(julianday('now') - 2440587.5) * 86400",
        "extract(epoch from clock_timestamp())::double precision",
    )
    query = query.replace("unixepoch()", "floor(extract(epoch from statement_timestamp()))::bigint")
    query = query.replace(
        "name COLLATE NOCASE",
        "translate(name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz') COLLATE \"C\"",
    )
    query = query.replace("LIMIT -1 OFFSET", "OFFSET")
    query = query.replace(" AS REAL)", " AS DOUBLE PRECISION)")
    query = query.replace("MAX(level - 1, 0)", "GREATEST(level - 1, 0)").replace(
        "MAX(next_start_at, ?)", "GREATEST(next_start_at, ?)"
    )
    # sqlite json_extract returns an SQL number for numeric cooldown timestamps.
    query = query.replace("json_extract(model_cooldowns, ?)", "gcli_cooldown(model_cooldowns, ?)")
    query = re.sub(r"json_extract\(([\w.]+),\s*'\$\.(\w+)'\)", r"(\1::jsonb ->> '\2')", query)
    query = query.replace("%", "%%")

    def placeholder(match):
        if match[0] == "?":
            return "%s"
        if match[1]:
            return f"%({match[1]})s"
        return match[0]

    return _tokens.sub(placeholder, query)


def parameters(values):
    if isinstance(values, dict):
        return {key: int(v) if type(v) is bool else v for key, v in values.items()}
    return tuple(int(v) if type(v) is bool else v for v in values)


@contextmanager
def safe_database_errors():
    # Driver DETAIL/CONTEXT can contain the rejected credential or API key row.
    try:
        yield
    except psycopg.DatabaseError as exc:
        constraint = exc.diag.constraint_name or ""
        raise type(exc)(f"{type(exc).__name__} {constraint}".strip()) from None


class Statement:
    def __init__(self, execute):
        self.execute = execute

    def __await__(self):
        return self.execute.__await__()

    async def __aenter__(self):
        self.result = await self.execute
        return self.result

    async def __aexit__(self, *_):
        await self.result.close()


class Session:
    row_factory = None

    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, values=()):
        return Statement(self._execute(query, values))

    async def _execute(self, query, values):
        if query.strip().upper() == "BEGIN IMMEDIATE":
            # Multi-step management mutations share the same transaction lock.
            # Request admission, account selection and accounting take narrower locks.
            query = "SELECT pg_advisory_xact_lock(72210439)"
        cursor = self.conn.cursor()
        if self.row_factory is not None:
            from psycopg.rows import dict_row

            cursor.row_factory = dict_row
        operation = "advisory" if query == "SELECT pg_advisory_xact_lock(72210440)" else "sql"
        with safe_database_errors(), measure_pg(operation):
            await cursor.execute(pg_query(query), parameters(values))
        return cursor

    async def executemany(self, query, rows):
        cursor = self.conn.cursor()
        with safe_database_errors():
            await cursor.executemany(pg_query(query), (parameters(row) for row in rows))
        return cursor

    async def execute_fetchall(self, query, values=()):
        async with self.execute(query, values) as result:
            return await result.fetchall()

    async def commit(self):
        with measure_pg("transaction"):
            await self.conn.commit()

    async def rollback(self):
        await self.conn.rollback()


_sync_pools = {}
_pool_lock = Lock()


def open_sync_pool(key):
    with _pool_lock:
        if key in _sync_pools:
            pool, refs = _sync_pools[key]
        else:
            dsn, schema = key
            pool = ConnectionPool(
                dsn,
                min_size=0,
                max_size=4,
                timeout=10,
                kwargs=connection_options(schema),
                open=True,
            )
            refs = 0
        _sync_pools[key] = (pool, refs + 1)


def close_sync_pool(key):
    with _pool_lock:
        pool, refs = _sync_pools[key]
        if refs > 1:
            _sync_pools[key] = (pool, refs - 1)
            return
        del _sync_pools[key]
    pool.close()


class SyncSession:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, values=()):
        with safe_database_errors():
            return self.conn.execute(pg_query(query), parameters(values))

    def commit(self):
        self.conn.commit()


@contextmanager
def sync_connection():
    key = main_dsn(), schema_name()
    with _pool_lock:
        if key not in _sync_pools:
            raise RuntimeError("PostgreSQL main store is not initialized")
        pool, _ = _sync_pools[key]
    with pool.connection() as conn:
        yield SyncSession(conn)


class PostgresDatabase:
    def __init__(self, dsn):
        self.schema = schema_name()
        self.pool = AsyncConnectionPool(
            dsn,
            min_size=0,
            max_size=6,
            timeout=10,
            kwargs=connection_options(self.schema),
            open=False,
        )

    @asynccontextmanager
    async def connect(self):
        async with AsyncExitStack() as stack:
            with measure_pg("pool"):
                conn = await stack.enter_async_context(self.pool.connection())
            try:
                yield Session(conn)
            finally:
                # Callers explicitly commit writes. Early returns discard tentative changes.
                with measure_pg("transaction"):
                    await conn.rollback()

    async def single_row(self, query, values):
        """Run one atomic statement; release its locks before returning to Python."""
        async with AsyncExitStack() as stack:
            with measure_pg("pool"):
                conn = await stack.enter_async_context(self.pool.connection())
            await conn.set_autocommit(True)
            try:
                with safe_database_errors(), measure_pg("sql"):
                    result = await conn.execute(query, values)
                    return await result.fetchone()
            finally:
                await conn.set_autocommit(False)

    async def execute_fetchall(self, query, values=()):
        async with self.connect() as db:
            return await db.execute_fetchall(query, values)
