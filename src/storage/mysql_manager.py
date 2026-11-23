"""MySQL main store using the shared credential, quota and billing repositories."""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from src.storage.main_mysql import MySQLDatabase, connection_args, sync_connection
from src.storage.recent_monitor_cache import RecentMonitorCache
from src.storage.refresh_import_queue import RefreshImportQueue
from src.storage.sqlite_manager import SQLiteManager
from log import log


DDL = Path(__file__).resolve().parents[2] / "scripts/mysql/main_tables.sql"


async def install_schema(conn):
    lock = hashlib.sha256(connection_args()["db"].encode()).hexdigest()[:32]
    async with conn.cursor() as cur:
        await cur.execute("SELECT GET_LOCK(%s, 30)", (f"gcli-schema-{lock}",))
        if (await cur.fetchone())[0] != 1:
            raise RuntimeError("Timed out waiting for MySQL schema initialization")
        try:
            await cur.execute("CREATE TABLE IF NOT EXISTS main_schema_versions (version INT PRIMARY KEY) ENGINE=InnoDB")
            await cur.execute("SELECT MAX(version) FROM main_schema_versions")
            version = (await cur.fetchone())[0]
            if version == 1:
                await conn.commit()
                return
            if version is not None:
                raise RuntimeError("Unsupported MySQL main schema version")
            for statement in DDL.read_text().split(";"):
                if statement.strip():
                    await cur.execute(statement)
            await cur.execute("""
                INSERT INTO api_keys (id,name,key_prefix,kind,status,currency,quota_period_start)
                VALUES ('env','环境配置密钥','env','environment','active',%s,%s)
                ON DUPLICATE KEY UPDATE id=api_keys.id
            """, (os.getenv("BILLING_CURRENCY", "CNY"), datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()))
            await cur.execute("INSERT INTO main_schema_versions VALUES (1)")
            await conn.commit()
        finally:
            await cur.execute("SELECT RELEASE_LOCK(%s)", (f"gcli-schema-{lock}",))


class MySQLManager(SQLiteManager):
    main_mysql = True

    async def initialize(self):
        async with self._lock:
            if self._initialized:
                return
            self._credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
            self._db_path = database = MySQLDatabase()
            await database.open()
            try:
                async with database.pool.acquire() as conn:
                    await install_schema(conn)
                self.refresh_import_queue = RefreshImportQueue(database, self._credentials_dir)
                await self._load_config_cache()
                if not self._config_loaded:
                    raise RuntimeError("MySQL configuration load failed")
                self.recent_cache = RecentMonitorCache(database, None, self.monitor_excluded_accounts())
                stats_dsn = os.getenv("MODEL_STATS_POSTGRES_DSN")
                if stats_dsn:
                    from src.storage.pg_model_stats import PostgresModelStats
                    self._pg_stats = await PostgresModelStats.open(stats_dsn)
                self._initialized = True
                log.info("MySQL main store ready")
            except BaseException:
                await database.close()
                raise

    async def close(self):
        if self._initialized:
            try:
                await super().close()
            finally:
                await self._db_path.close()

    @contextmanager
    def _claim_transaction(self):
        with sync_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _call_record_aggregation_source(since_ts, until_ts):
        return "call_records"
