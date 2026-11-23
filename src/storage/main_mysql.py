"""MySQL 8.4 connections for the shared main-store repositories."""
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache
import os
import re
import ssl
from urllib.parse import urlsplit, unquote

import aiomysql
import pymysql

from src.storage.main_postgres import Statement


def connection_args():
    url = urlsplit(os.environ["MAIN_MYSQL_DSN"].strip())
    if url.scheme != "mysql" or not url.hostname or not url.path.strip("/") or url.fragment or url.query:
        raise ValueError("MAIN_MYSQL_DSN must be mysql://user:password@host:port/database")
    ca_file = os.getenv("MAIN_MYSQL_SSL_CA", "").strip()
    options = {"ssl": ssl.create_default_context(cafile=ca_file)} if ca_file else {}
    return dict(**options, host=url.hostname, port=url.port or 3306, user=unquote(url.username or ""),
                password=unquote(url.password or ""), db=unquote(url.path[1:]), charset="utf8mb4",
                autocommit=False, connect_timeout=5,
                init_command="SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")


# Repositories bind values separately. Preserve literals when translating markers and identifiers.
_tokens = re.compile(r"""'(?:''|[^'])*'|"(?:""|[^"])*"|`[^`]*`|:([a-zA-Z_]\w*)|\?|\b(key|keys|groups|rank|condition|usage|cursor)\b""", re.I)


@lru_cache(maxsize=1024)
def mysql_query(query):
    if query.strip().upper() == "BEGIN IMMEDIATE":
        # Emulate SQLite's serialization of multi-step read/modify/write operations across workers.
        return "SELECT id FROM main_write_lock WHERE id=1 FOR UPDATE"
    query = query.replace("(julianday('now') - 2440587.5) * 86400", "UNIX_TIMESTAMP(NOW(6))")
    query = re.sub(r"SELECT COALESCE\(MAX\(routing_turn\), 0\) \+ 1\s+FROM antigravity_credentials",
                   "SELECT COALESCE(tail,0)+1 FROM (SELECT MAX(routing_turn) AS tail FROM antigravity_credentials) AS turns", query)
    query = query.replace("unixepoch()", "UNIX_TIMESTAMP()").replace("RANDOM()", "RAND()")
    query = query.replace("COLLATE NOCASE", "COLLATE utf8mb4_0900_as_ci")
    query = query.replace("LIMIT -1 OFFSET", "LIMIT 18446744073709551615 OFFSET")
    query = re.sub(r" AS (?:BIGINT|INTEGER)\)", " AS SIGNED)", query, flags=re.I)
    query = re.sub(r" AS (?:REAL|NUMERIC)\)", " AS DECIMAL(40,12))", query, flags=re.I)
    query = query.replace("MAX(level - 1, 0)", "GREATEST(level - 1, 0)").replace(
        "MAX(next_start_at, ?)", "GREATEST(next_start_at, ?)")
    query = query.replace("json_type(model_cooldowns, :path) IN ('integer', 'real')",
                          "JSON_TYPE(JSON_EXTRACT(model_cooldowns, :path)) IN ('INTEGER','DOUBLE','DECIMAL')")
    query = re.sub(r"ON CONFLICT\s*\((\w+)\)\s*DO NOTHING", r"ON DUPLICATE KEY UPDATE \1=\1", query, flags=re.I)
    query = re.sub(r"ON CONFLICT\s*\([\w,\s]+\)\s*DO UPDATE SET", "ON DUPLICATE KEY UPDATE", query, flags=re.I)
    query = re.sub(r"\bexcluded\.(\w+)", r"VALUES(\1)", query, flags=re.I)
    query = query.replace("%", "%%")

    def token(m):
        if m[0] == "?":
            return "%s"
        if m[1]:
            return f"%({m[1]})s"
        if m[2]:
            return f"`{m[2]}`"
        return m[0]

    return _tokens.sub(token, query).replace("ON DUPLICATE `KEY`", "ON DUPLICATE KEY")


@contextmanager
def safe_database_errors():
    # Driver errors can otherwise echo credential JSON, config values or an API key.
    try:
        yield
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args else 0
        constraint = ""
        if code == 1062:
            match = re.search(r"for key '[\w.]+\.(\w+)'$", str(exc.args[1]))
            constraint = "UNIQUE " + (match[1] if match else "constraint")
        raise type(exc)(code, constraint or type(exc).__name__) from None


class Session:
    def __init__(self, conn, row_factory=None):
        self.conn = conn
        self.row_factory = row_factory

    def execute(self, query, values=()):
        return Statement(self._execute(query, values))

    async def _execute(self, query, values):
        cursor = await self.conn.cursor(aiomysql.DictCursor if self.row_factory else aiomysql.Cursor)
        with safe_database_errors():
            await cursor.execute(mysql_query(query), values)
        return cursor

    async def executemany(self, query, rows):
        cursor = await self.conn.cursor()
        with safe_database_errors():
            await cursor.executemany(mysql_query(query), list(rows))
        return cursor

    async def commit(self):
        await self.conn.commit()

    async def rollback(self):
        await self.conn.rollback()


class MySQLDatabase:
    async def open(self):
        self.pool = await aiomysql.create_pool(
            **connection_args(), minsize=1, maxsize=32, pool_recycle=1800,
        )

    async def close(self):
        self.pool.close()
        await self.pool.wait_closed()

    @asynccontextmanager
    async def connect(self, *, row_factory=None):
        async with self.pool.acquire() as conn:
            try:
                yield Session(conn, row_factory)
            finally:
                if not conn.closed:
                    await conn.rollback()


class SyncSession:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, values=()):
        cursor = self.conn.cursor()
        with safe_database_errors():
            cursor.execute(mysql_query(query), values)
        return cursor

    def commit(self):
        self.conn.commit()


@contextmanager
def sync_connection():
    with pymysql.connect(**connection_args()) as conn:
        try:
            yield SyncSession(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
