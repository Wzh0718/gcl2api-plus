"""
SQLite 存储管理器
"""

import asyncio
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import aiosqlite

from log import log


FAIR_ROUTING_CATCHUP_WINDOW = 32


def _active_model_cooldowns(
    model_cooldowns: Dict[str, Any], current_time: float
) -> Dict[str, float]:
    """Return only cooldowns that still block a model."""
    if not isinstance(model_cooldowns, dict):
        return {}
    return {
        model: float(until)
        for model, until in model_cooldowns.items()
        if isinstance(until, (int, float)) and until > current_time
    }


def _visible_error_codes(
    error_codes: List[Any], *, mode: str, active_cooldowns: Dict[str, float]
) -> List[Any]:
    """Hide transient Antigravity 429 after its model cooldown expires."""
    codes = list(error_codes) if isinstance(error_codes, list) else []
    if mode != "antigravity" or active_cooldowns:
        return codes
    return [code for code in codes if str(code) != "429"]


class SQLiteManager:
    """SQLite 数据库管理器"""

    # 状态字段常量
    STATE_FIELDS = {
        "error_codes",
        "error_messages",
        "disabled",
        "last_success",
        "user_email",
        "model_cooldowns",
        "preview",
        "tier",
        "enable_credit",
    }

    # 所有必需的列定义（用于自动校验和修复）
    REQUIRED_COLUMNS = {
        "credentials": [
            ("disabled", "INTEGER DEFAULT 0"),
            ("error_codes", "TEXT DEFAULT '[]'"),
            ("error_messages", "TEXT DEFAULT '[]'"),
            ("last_success", "REAL"),
            ("user_email", "TEXT"),
            ("model_cooldowns", "TEXT DEFAULT '{}'"),
            ("preview", "INTEGER DEFAULT 1"),
            ("tier", "TEXT DEFAULT 'pro'"),
            ("rotation_order", "INTEGER DEFAULT 0"),
            ("call_count", "INTEGER DEFAULT 0"),
            ("created_at", "REAL DEFAULT (unixepoch())"),
            ("updated_at", "REAL DEFAULT (unixepoch())")
        ],
        "antigravity_credentials": [
            ("disabled", "INTEGER DEFAULT 0"),
            ("error_codes", "TEXT DEFAULT '[]'"),
            ("error_messages", "TEXT DEFAULT '[]'"),
            ("last_success", "REAL"),
            ("user_email", "TEXT"),
            ("model_cooldowns", "TEXT DEFAULT '{}'"),
            ("tier", "TEXT DEFAULT 'pro'"),
            ("enable_credit", "INTEGER DEFAULT 0"),
            ("proxy_mode", "TEXT DEFAULT 'inherit'"),
            ("proxy_url", "TEXT"),
            ("proxy_group_id", "INTEGER"),
            ("bound_proxy_node_id", "INTEGER"),
            ("rotation_order", "INTEGER DEFAULT 0"),
            ("call_count", "INTEGER DEFAULT 0"),
            ("created_at", "REAL DEFAULT (unixepoch())"),
            ("updated_at", "REAL DEFAULT (unixepoch())")
        ],
        "credential_model_stats": [
            ("upstream_total_seconds", "REAL NOT NULL DEFAULT 0"),
            ("upstream_timed_count", "INTEGER NOT NULL DEFAULT 0"),
            ("gateway_total_seconds", "REAL NOT NULL DEFAULT 0"),
            ("gateway_timed_count", "INTEGER NOT NULL DEFAULT 0")
        ],
        "credential_model_stats_daily": [
            ("upstream_total_seconds", "REAL NOT NULL DEFAULT 0"),
            ("upstream_timed_count", "INTEGER NOT NULL DEFAULT 0"),
            ("gateway_total_seconds", "REAL NOT NULL DEFAULT 0"),
            ("gateway_timed_count", "INTEGER NOT NULL DEFAULT 0")
        ]
    }

    def __init__(self):
        self._db_path = None
        self._credentials_dir = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._selection_lock = asyncio.Lock()
        self._success_write_lock = asyncio.Lock()
        self._stats_write_lock = asyncio.Lock()

        # 内存配置缓存 - 初始化时加载一次
        self._config_cache: Dict[str, Any] = {}
        self._config_loaded = False

    async def initialize(self) -> None:
        """初始化 SQLite 数据库"""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            try:
                # 获取凭证目录
                self._credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
                self._db_path = os.path.join(self._credentials_dir, "credentials.db")

                # 确保目录存在
                os.makedirs(self._credentials_dir, exist_ok=True)

                # 创建数据库和表
                async with aiosqlite.connect(self._db_path) as db:
                    # 启用 WAL 模式（提升并发性能）
                    await db.execute("PRAGMA journal_mode=WAL")
                    await db.execute("PRAGMA foreign_keys=ON")

                    # 检查并自动修复数据库结构
                    await self._ensure_schema_compatibility(db)

                    # 创建表
                    await self._create_tables(db)

                    # 旧版托管密钥只有不可逆哈希；新增密文字段后，新建密钥可在
                    # 管理面板中安全取回。旧记录保持 NULL 并明确标记不可恢复。
                    await self._migrate_api_key_secret_ciphertext(db)

                    # billing_daily 的联合主键需要包含调用方密钥。旧表不能通过
                    # ALTER COLUMN 修改主键，因此使用一次性事务迁移并回填环境密钥。
                    await self._migrate_billing_daily_api_keys(db)

                    # 已用额度同时记录金额和 Tokens。旧库补列后按当前额度周期
                    # 的日账单回填一次，后续由账单事务精确累计和重置。
                    await self._migrate_api_key_quota_tokens(db)

                    # 修复可能包含路径的凭证文件名
                    await self._repair_credential_filenames(db)

                    # 旧版本只记录账单、不推进 call_count。使用已有账单请求数
                    # 初始化调度负载，让历史使用较少的账号有界地优先补齐。
                    await self._backfill_credential_call_counts(db)

                    await db.commit()

                # 加载配置到内存
                await self._load_config_cache()

                self._initialized = True
                log.info(f"SQLite storage initialized at {self._db_path}")

            except Exception as e:
                log.error(f"Error initializing SQLite: {e}")
                raise

    async def _ensure_schema_compatibility(self, db: aiosqlite.Connection) -> None:
        """
        确保数据库结构兼容，自动修复缺失的列
        """
        try:
            # 检查每个表
            for table_name, columns in self.REQUIRED_COLUMNS.items():
                # 检查表是否存在
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,)
                ) as cursor:
                    if not await cursor.fetchone():
                        log.debug(f"Table {table_name} does not exist, will be created")
                        continue

                # 获取现有列
                async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                    existing_columns = {row[1] for row in await cursor.fetchall()}

                # 添加缺失的列
                added_count = 0
                for col_name, col_def in columns:
                    if col_name not in existing_columns:
                        try:
                            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")
                            log.info(f"Added missing column {table_name}.{col_name}")
                            added_count += 1
                        except Exception as e:
                            log.error(f"Failed to add column {table_name}.{col_name}: {e}")

                if added_count > 0:
                    log.info(f"Table {table_name}: added {added_count} missing column(s)")

        except Exception as e:
            log.error(f"Error ensuring schema compatibility: {e}")
            # 不抛出异常，允许继续初始化

    async def _create_tables(self, db: aiosqlite.Connection):
        """创建数据库表和索引"""
        # 凭证表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE NOT NULL,
                credential_data TEXT NOT NULL,

                -- 状态字段
                disabled INTEGER DEFAULT 0,
                error_codes TEXT DEFAULT '[]',
                error_messages TEXT DEFAULT '[]',
                last_success REAL,
                user_email TEXT,

                -- 模型级 CD 支持 (JSON: {model_name: cooldown_timestamp})
                model_cooldowns TEXT DEFAULT '{}',

                -- preview 状态 (只对 geminicli 有效，默认为 true)
                preview INTEGER DEFAULT 1,

                -- tier 状态 (只对 geminicli 有效，默认为 pro)
                tier TEXT DEFAULT 'pro',

                -- 轮换相关
                rotation_order INTEGER DEFAULT 0,
                call_count INTEGER DEFAULT 0,

                -- 时间戳
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            )
        """)

        # Antigravity 凭证表（结构相同但独立存储）
        await db.execute("""
            CREATE TABLE IF NOT EXISTS antigravity_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT UNIQUE NOT NULL,
                credential_data TEXT NOT NULL,

                -- 状态字段
                disabled INTEGER DEFAULT 0,
                error_codes TEXT DEFAULT '[]',
                error_messages TEXT DEFAULT '[]',
                last_success REAL,
                user_email TEXT,

                -- 模型级 CD 支持 (JSON: {model_name: cooldown_timestamp})
                model_cooldowns TEXT DEFAULT '{}',

                -- tier 状态 (默认为 pro)
                tier TEXT DEFAULT 'pro',

                -- 是否启用信用额度模式（仅 antigravity，有效值 0/1）
                enable_credit INTEGER DEFAULT 0,

                -- 账号级代理：inherit/custom/direct/group
                proxy_mode TEXT NOT NULL DEFAULT 'inherit',
                proxy_url TEXT,
                proxy_group_id INTEGER,
                bound_proxy_node_id INTEGER,

                -- 轮换相关
                rotation_order INTEGER DEFAULT 0,
                call_count INTEGER DEFAULT 0,

                -- 时间戳
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            )
        """)

        # 创建索引 - 普通凭证表
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_disabled
            ON credentials(disabled)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_rotation_order
            ON credentials(rotation_order)
        """)

        # 创建索引 - Antigravity 凭证表
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ag_disabled
            ON antigravity_credentials(disabled)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ag_rotation_order
            ON antigravity_credentials(rotation_order)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ag_proxy_group
            ON antigravity_credentials(proxy_group_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_ag_bound_proxy_node
            ON antigravity_credentials(bound_proxy_node_id)
        """)

        # 代理组配置独立于 OAuth 凭证 JSON，支持运行时导入和切换。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS proxy_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                strategy TEXT NOT NULL DEFAULT 'round_robin',
                enabled INTEGER NOT NULL DEFAULT 1,
                cursor INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT (unixepoch()),
                updated_at REAL NOT NULL DEFAULT (unixepoch())
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS proxy_group_nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                proxy_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                failure_count INTEGER NOT NULL DEFAULT 0,
                cooldown_until REAL,
                last_used_at REAL,
                created_at REAL NOT NULL DEFAULT (unixepoch()),
                updated_at REAL NOT NULL DEFAULT (unixepoch()),
                UNIQUE(group_id, proxy_url),
                FOREIGN KEY(group_id) REFERENCES proxy_groups(id) ON DELETE CASCADE
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_proxy_group_nodes_group
            ON proxy_group_nodes(group_id, enabled, id)
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS antigravity_account_health (
                credential_name TEXT PRIMARY KEY,
                egress_ip TEXT,
                egress_country TEXT,
                binding_status TEXT NOT NULL DEFAULT 'unchecked',
                eligibility_status TEXT NOT NULL DEFAULT 'unchecked',
                eligibility_reason TEXT,
                eligibility_checked_at REAL,
                last_403_category TEXT,
                last_403_reason TEXT,
                last_403_at REAL,
                updated_at REAL NOT NULL DEFAULT (unixepoch())
            )
        """)

        # 配置表
        await db.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL DEFAULT (unixepoch())
            )
        """)

        # 下游 API 密钥保留哈希用于请求认证，密文用于面板内后续复制。固定 env
        # 行代表仍由 API_PASSWORD/PASSWORD 提供的兼容密钥，不保存其原文或哈希。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                key_prefix TEXT NOT NULL UNIQUE,
                secret_hash TEXT UNIQUE,
                secret_ciphertext TEXT,
                kind TEXT NOT NULL CHECK(kind IN ('managed', 'environment')),
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'disabled', 'revoked')),
                quota_amount TEXT,
                quota_used TEXT NOT NULL DEFAULT '0.00000000',
                quota_tokens_used INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'CNY',
                reset_mode TEXT NOT NULL DEFAULT 'manual'
                    CHECK(reset_mode IN ('manual', 'monthly')),
                quota_period_start TEXT NOT NULL,
                expires_at REAL,
                last_used_at REAL,
                last_reset_at REAL,
                revoked_at REAL,
                created_at REAL NOT NULL DEFAULT (unixepoch()),
                updated_at REAL NOT NULL DEFAULT (unixepoch()),
                CHECK(
                    (kind = 'environment' AND secret_hash IS NULL)
                    OR (kind = 'managed' AND secret_hash IS NOT NULL)
                )
            )
        """)
        await db.execute(
            """INSERT OR IGNORE INTO api_keys(
                   id, name, key_prefix, secret_hash, kind, status,
                   quota_amount, quota_used, currency, reset_mode, quota_period_start
               ) VALUES ('env', '环境配置密钥', 'env', NULL, 'environment', 'active',
                         NULL, '0.00000000', ?, 'manual', ?)""",
            (
                os.getenv("BILLING_CURRENCY", "CNY") or "CNY",
                datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            ),
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_key_quota_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('manual_reset', 'monthly_reset')),
                amount_before TEXT NOT NULL,
                amount_after TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (unixepoch()),
                FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE RESTRICT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_keys_status
            ON api_keys(status, kind, created_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_key_quota_events_key
            ON api_key_quota_events(api_key_id, created_at)
        """)

        # 计费相关表只属于 SQLite 单库版。费用使用文本保存，避免 SQLite REAL
        # 在八位小数累计时引入二进制浮点误差。
        await db.execute("""
            CREATE TABLE IF NOT EXISTS billing_prices (
                model TEXT PRIMARY KEY,
                input_price TEXT NOT NULL DEFAULT '0.00000000',
                output_price TEXT NOT NULL DEFAULT '0.00000000',
                cache_price TEXT NOT NULL DEFAULT '0.00000000',
                currency TEXT NOT NULL DEFAULT 'CNY',
                updated_at REAL NOT NULL DEFAULT (unixepoch())
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS billing_daily (
                billing_date TEXT NOT NULL,
                api_key_id TEXT NOT NULL DEFAULT 'env',
                credential_name TEXT NOT NULL,
                model TEXT NOT NULL,
                currency TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_tokens INTEGER NOT NULL DEFAULT 0,
                thought_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_cost TEXT NOT NULL DEFAULT '0.00000000',
                output_cost TEXT NOT NULL DEFAULT '0.00000000',
                cache_cost TEXT NOT NULL DEFAULT '0.00000000',
                total_cost TEXT NOT NULL DEFAULT '0.00000000',
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                unknown_usage_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT (unixepoch()),
                PRIMARY KEY (billing_date, api_key_id, credential_name, model, currency),
                FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE RESTRICT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS billing_request_dedupe (
                request_id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_billing_daily_date
            ON billing_daily(billing_date)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_billing_daily_credential
            ON billing_daily(credential_name, billing_date)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_billing_daily_model
            ON billing_daily(model, billing_date)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_billing_dedupe_expiry
            ON billing_request_dedupe(expires_at)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS credential_model_stats (
                mode TEXT NOT NULL,
                credential_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                last_status INTEGER,
                upstream_total_seconds REAL NOT NULL DEFAULT 0,
                upstream_timed_count INTEGER NOT NULL DEFAULT 0,
                gateway_total_seconds REAL NOT NULL DEFAULT 0,
                gateway_timed_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT (unixepoch()),
                PRIMARY KEY (mode, credential_name, model_name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS credential_model_stats_daily (
                mode TEXT NOT NULL,
                stat_date TEXT NOT NULL,
                credential_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                last_status INTEGER,
                upstream_total_seconds REAL NOT NULL DEFAULT 0,
                upstream_timed_count INTEGER NOT NULL DEFAULT 0,
                gateway_total_seconds REAL NOT NULL DEFAULT 0,
                gateway_timed_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT (unixepoch()),
                PRIMARY KEY (mode, stat_date, credential_name, model_name)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_model_stats_daily_date
            ON credential_model_stats_daily(mode, stat_date)
        """)

        log.debug("SQLite tables and indexes created")

    async def _migrate_api_key_secret_ciphertext(
        self, db: aiosqlite.Connection
    ) -> None:
        """Add encrypted-secret storage without fabricating legacy plaintext."""
        async with db.execute("PRAGMA table_info(api_keys)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if columns and "secret_ciphertext" not in columns:
            await db.execute(
                "ALTER TABLE api_keys ADD COLUMN secret_ciphertext TEXT"
            )
            log.info("Added encrypted secret storage to api_keys")

    async def _migrate_billing_daily_api_keys(self, db: aiosqlite.Connection) -> None:
        """Upgrade the legacy billing primary key without losing aggregates."""
        async with db.execute("PRAGMA table_info(billing_daily)") as cursor:
            columns = await cursor.fetchall()
        if not columns:
            return
        if any(row[1] == "api_key_id" for row in columns):
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_billing_daily_api_key ON billing_daily(api_key_id, billing_date)"
            )
            return

        await db.execute("""
            CREATE TABLE billing_daily_v2 (
                billing_date TEXT NOT NULL,
                api_key_id TEXT NOT NULL DEFAULT 'env',
                credential_name TEXT NOT NULL,
                model TEXT NOT NULL,
                currency TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_tokens INTEGER NOT NULL DEFAULT 0,
                thought_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_cost TEXT NOT NULL DEFAULT '0.00000000',
                output_cost TEXT NOT NULL DEFAULT '0.00000000',
                cache_cost TEXT NOT NULL DEFAULT '0.00000000',
                total_cost TEXT NOT NULL DEFAULT '0.00000000',
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                unknown_usage_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT (unixepoch()),
                PRIMARY KEY (billing_date, api_key_id, credential_name, model, currency),
                FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE RESTRICT
            )
        """)
        await db.execute("""
            INSERT INTO billing_daily_v2(
                billing_date, api_key_id, credential_name, model, currency,
                input_tokens, output_tokens, cache_tokens, thought_tokens, total_tokens,
                input_cost, output_cost, cache_cost, total_cost,
                success_count, failed_count, unknown_usage_count, updated_at
            )
            SELECT billing_date, 'env', credential_name, model, currency,
                   input_tokens, output_tokens, cache_tokens, thought_tokens, total_tokens,
                   input_cost, output_cost, cache_cost, total_cost,
                   success_count, failed_count, unknown_usage_count, updated_at
            FROM billing_daily
        """)
        await db.execute("DROP TABLE billing_daily")
        await db.execute("ALTER TABLE billing_daily_v2 RENAME TO billing_daily")
        await db.execute("CREATE INDEX idx_billing_daily_date ON billing_daily(billing_date)")
        await db.execute(
            "CREATE INDEX idx_billing_daily_credential ON billing_daily(credential_name, billing_date)"
        )
        await db.execute("CREATE INDEX idx_billing_daily_model ON billing_daily(model, billing_date)")
        await db.execute("CREATE INDEX idx_billing_daily_api_key ON billing_daily(api_key_id, billing_date)")
        log.info("Migrated billing_daily to API-key-aware schema")

    async def _migrate_api_key_quota_tokens(
        self, db: aiosqlite.Connection
    ) -> None:
        """Add the per-quota-period token counter and backfill existing keys once."""
        async with db.execute("PRAGMA table_info(api_keys)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if not columns or "quota_tokens_used" in columns:
            return

        await db.execute(
            "ALTER TABLE api_keys ADD COLUMN quota_tokens_used INTEGER NOT NULL DEFAULT 0"
        )
        await db.execute(
            """UPDATE api_keys
               SET quota_tokens_used = COALESCE((
                   SELECT SUM(billing_daily.total_tokens)
                   FROM billing_daily
                   WHERE billing_daily.api_key_id = api_keys.id
                     AND billing_daily.billing_date >= api_keys.quota_period_start
               ), 0)"""
        )
        environment_costs: Dict[str, Decimal] = {}
        async with db.execute(
            """SELECT api_keys.id, billing_daily.total_cost
               FROM api_keys
               JOIN billing_daily ON billing_daily.api_key_id = api_keys.id
               WHERE api_keys.kind = 'environment'
                 AND billing_daily.billing_date >= api_keys.quota_period_start"""
        ) as cursor:
            async for api_key_id, total_cost in cursor:
                environment_costs[api_key_id] = (
                    environment_costs.get(api_key_id, Decimal("0"))
                    + Decimal(str(total_cost or "0"))
                )
        for api_key_id, total_cost in environment_costs.items():
            await db.execute(
                "UPDATE api_keys SET quota_used = ? WHERE id = ?",
                (
                    f"{total_cost.quantize(Decimal('0.00000001')):.8f}",
                    api_key_id,
                ),
            )
        log.info("Added and backfilled quota token counters for API keys")

    async def _backfill_credential_call_counts(
        self, db: aiosqlite.Connection
    ) -> None:
        """Seed fair-routing counters from persisted billing request totals."""
        await db.execute(
            """WITH usage AS (
                   SELECT credential_name,
                          SUM(success_count + failed_count) AS request_count
                   FROM billing_daily
                   GROUP BY credential_name
               ), peak AS (
                   SELECT COALESCE(MAX(request_count), 0) AS max_request_count
                   FROM usage
               )
               UPDATE antigravity_credentials
               SET call_count = MAX(
                   COALESCE(call_count, 0),
                   COALESCE((
                       SELECT request_count
                       FROM usage
                       WHERE usage.credential_name =
                             antigravity_credentials.filename
                   ), 0),
                   MAX(
                       (SELECT max_request_count FROM peak) - ?,
                       0
                   )
               )""",
            (FAIR_ROUTING_CATCHUP_WINDOW,),
        )

    async def _repair_credential_filenames(self, db: aiosqlite.Connection):
        """
        修复凭证数据库中可能包含路径的文件名，确保所有文件名都是 basename
        """
        try:
            repaired_count = 0

            # 修复 credentials 表
            async with db.execute("SELECT filename FROM credentials") as cursor:
                rows = await cursor.fetchall()
                for (filename,) in rows:
                    basename = os.path.basename(filename)
                    if basename != filename:
                        # 检查是否会产生冲突
                        async with db.execute(
                            "SELECT COUNT(*) FROM credentials WHERE filename = ?",
                            (basename,)
                        ) as check_cursor:
                            count = (await check_cursor.fetchone())[0]

                        if count == 0:
                            # 无冲突，直接更新
                            await db.execute(
                                "UPDATE credentials SET filename = ? WHERE filename = ?",
                                (basename, filename)
                            )
                            repaired_count += 1
                            log.info(f"Repaired credential filename: {filename} -> {basename}")
                        else:
                            # 有冲突，删除带路径的旧记录（保留 basename 的记录）
                            await db.execute(
                                "DELETE FROM credentials WHERE filename = ?",
                                (filename,)
                            )
                            repaired_count += 1
                            log.warning(f"Removed duplicate credential with path: {filename} (kept {basename})")

            # 修复 antigravity_credentials 表
            async with db.execute("SELECT filename FROM antigravity_credentials") as cursor:
                rows = await cursor.fetchall()
                for (filename,) in rows:
                    basename = os.path.basename(filename)
                    if basename != filename:
                        # 检查是否会产生冲突
                        async with db.execute(
                            "SELECT COUNT(*) FROM antigravity_credentials WHERE filename = ?",
                            (basename,)
                        ) as check_cursor:
                            count = (await check_cursor.fetchone())[0]

                        if count == 0:
                            # 无冲突，直接更新
                            await db.execute(
                                "UPDATE antigravity_credentials SET filename = ? WHERE filename = ?",
                                (basename, filename)
                            )
                            repaired_count += 1
                            log.info(f"Repaired antigravity credential filename: {filename} -> {basename}")
                        else:
                            # 有冲突，删除带路径的旧记录（保留 basename 的记录）
                            await db.execute(
                                "DELETE FROM antigravity_credentials WHERE filename = ?",
                                (filename,)
                            )
                            repaired_count += 1
                            log.warning(f"Removed duplicate antigravity credential with path: {filename} (kept {basename})")

            if repaired_count > 0:
                log.info(f"Repaired {repaired_count} credential filename(s)")
            else:
                log.debug("No credential filenames need repair")

        except Exception as e:
            log.error(f"Error repairing credential filenames: {e}")
            # 不抛出异常，允许继续初始化

    async def _load_config_cache(self):
        """加载配置到内存缓存（仅在初始化时调用一次）"""
        if self._config_loaded:
            return

        try:
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute("SELECT key, value FROM config") as cursor:
                    rows = await cursor.fetchall()

                for key, value in rows:
                    try:
                        self._config_cache[key] = json.loads(value)
                    except json.JSONDecodeError:
                        self._config_cache[key] = value

            self._config_loaded = True
            log.debug(f"Loaded {len(self._config_cache)} config items into cache")

        except Exception as e:
            log.error(f"Error loading config cache: {e}")
            self._config_cache = {}

    async def close(self) -> None:
        """关闭数据库连接"""
        self._initialized = False
        log.debug("SQLite storage closed")

    def _ensure_initialized(self):
        """确保已初始化"""
        if not self._initialized:
            raise RuntimeError("SQLite manager not initialized")

    @staticmethod
    def _money_to_e8(value: Any) -> int:
        """Convert an eight-decimal SQLite text amount to an exact integer."""
        return int((Decimal(str(value or "0")) * Decimal(100_000_000)).to_integral_value())

    async def _register_money_functions(self, db: aiosqlite.Connection) -> None:
        await db.create_function("money_e8", 1, self._money_to_e8, deterministic=True)

    def _get_table_name(self, mode: str) -> str:
        """根据 mode 获取对应的表名"""
        if mode == "antigravity":
            return "antigravity_credentials"
        elif mode == "geminicli":
            return "credentials"
        else:
            raise ValueError(f"Invalid mode: {mode}. Must be 'geminicli' or 'antigravity'")

    # ============ SQL 方法 ============

    async def get_next_available_credential(
        self,
        mode: str = "geminicli",
        model_name: Optional[str] = None,
        exclude_filenames: Optional[List[str]] = None,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        获取一个可用凭证（负载均衡）
        - 未禁用
        - 如果提供了 model_name，还会检查模型级冷却和preview状态
        - GeminiCLI 保持随机选择；Antigravity 使用原子最少使用选择

        Args:
            mode: 凭证模式 ("geminicli" 或 "antigravity")
            model_name: 完整模型名（如 "gemini-2.0-flash-exp", "gemini-3-flash-preview"）
        """
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(mode)
            excluded = [
                os.path.basename(str(name))
                for name in (exclude_filenames or [])
                if str(name).strip()
            ]
            try:
                candidate_limit = min(
                    max(int(os.getenv("CREDENTIAL_CANDIDATE_LIMIT", "32")), 1),
                    32,
                )
            except ValueError:
                candidate_limit = 32

            if mode == "antigravity":
                # 排队发生在打开 aiosqlite 连接之前，避免同进程高并发同时
                # 占用大量 FD，并绕开 SQLite busy_timeout 的退避延迟。
                async with self._selection_lock:
                    return await self._get_next_antigravity_credential(
                        model_name=model_name,
                        excluded=excluded,
                        candidate_limit=candidate_limit,
                    )

            async with aiosqlite.connect(self._db_path) as db:
                current_time = time.time()
                params: List[Any] = []
                where_sql = "disabled = 0"
                order_sql = "RANDOM()"
                if model_name:
                    json_path = '$."' + model_name.replace("\\", "\\\\").replace('"', '\\"') + '"'
                    where_sql += " AND (json_extract(model_cooldowns, ?) IS NULL OR json_extract(model_cooldowns, ?) <= ?)"
                    params.extend((json_path, json_path, current_time))
                    is_preview_model = "preview" in model_name.lower()
                    if is_preview_model:
                        where_sql += " AND preview = 1"
                    else:
                        order_sql = "CASE WHEN preview = 0 THEN 0 ELSE 1 END, RANDOM()"
                if excluded:
                    where_sql += f" AND filename NOT IN ({','.join('?' for _ in excluded)})"
                    params.extend(excluded)
                async with db.execute(f"""
                    SELECT filename, credential_data, model_cooldowns, preview
                    FROM {table_name}
                    WHERE {where_sql}
                    ORDER BY {order_sql}
                    LIMIT {candidate_limit}
                """, params) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    return None
                filename, credential_json, _, _ = row
                return filename, json.loads(credential_json)

        except Exception as e:
            log.error(f"Error getting next available credential (mode={mode}, model_name={model_name}): {e}")
            return None

    async def _get_next_antigravity_credential(
        self,
        *,
        model_name: Optional[str],
        excluded: List[str],
        candidate_limit: int,
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Atomically select and count the least-used Antigravity account."""
        attempted = set(excluded)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            for _ in range(candidate_limit):
                current_time = time.time()
                params: List[Any] = [current_time]
                where_sql = """disabled = 0
                    AND (
                        COALESCE(proxy_mode, 'inherit') <> 'group'
                        OR EXISTS (
                            SELECT 1
                            FROM proxy_groups pg
                            JOIN proxy_group_nodes pn ON pn.group_id = pg.id
                            WHERE pg.id = proxy_group_id
                              AND pg.enabled = 1
                              AND pn.enabled = 1
                              AND (pn.cooldown_until IS NULL OR pn.cooldown_until <= ?)
                        )
                    )"""
                if model_name:
                    json_path = '$."' + model_name.replace("\\", "\\\\").replace('"', '\\"') + '"'
                    where_sql += " AND (json_extract(model_cooldowns, ?) IS NULL OR json_extract(model_cooldowns, ?) <= ?)"
                    params.extend((json_path, json_path, current_time))
                if attempted:
                    attempted_list = sorted(attempted)
                    where_sql += f" AND filename NOT IN ({','.join('?' for _ in attempted_list)})"
                    params.extend(attempted_list)

                await db.execute("BEGIN IMMEDIATE")
                try:
                    async with db.execute(f"""
                        SELECT filename, credential_data, model_cooldowns,
                               enable_credit, proxy_mode, proxy_url,
                               proxy_group_id
                        FROM antigravity_credentials
                        WHERE {where_sql}
                        ORDER BY COALESCE(call_count, 0) ASC,
                                 COALESCE(rotation_order, 0) ASC,
                                 filename ASC
                        LIMIT 1
                    """, params) as cursor:
                        row = await cursor.fetchone()
                    if not row:
                        await db.rollback()
                        return None
                    filename = row[0]
                    await db.execute(
                        """UPDATE antigravity_credentials
                           SET call_count = COALESCE(call_count, 0) + 1,
                               updated_at = unixepoch()
                           WHERE filename = ?""",
                        (filename,),
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

                filename, credential_json, _, enable_credit, proxy_mode, proxy_url, proxy_group_id = row
                credential_data = json.loads(credential_json)
                credential_data["enable_credit"] = bool(enable_credit)
                credential_data["proxy_mode"] = proxy_mode or "inherit"
                credential_data["proxy_url"] = proxy_url
                credential_data["proxy_group_id"] = proxy_group_id
                if credential_data["proxy_mode"] == "group":
                    if not proxy_group_id:
                        attempted.add(filename)
                        continue
                    network = await self.resolve_credential_network(
                        filename, mode="antigravity"
                    )
                    if not network.get("proxy_url"):
                        attempted.add(filename)
                        continue
                    credential_data.update(network)
                return filename, credential_data
        return None

    async def mark_credential_selected(
        self,
        filename: str,
        *,
        mode: str = "antigravity",
        expected_call_count: Optional[int] = None,
    ) -> bool:
        """Atomically increment one account's fair-routing counter."""
        self._ensure_initialized()
        filename = os.path.basename(filename)
        table_name = self._get_table_name(mode)
        where_sql = "filename = ? AND disabled = 0"
        params: List[Any] = [filename]
        if expected_call_count is not None:
            where_sql += " AND COALESCE(call_count, 0) = ?"
            params.append(max(int(expected_call_count), 0))

        try:
            async with self._selection_lock:
                async with aiosqlite.connect(self._db_path) as db:
                    await db.execute("PRAGMA busy_timeout = 5000")
                    result = await db.execute(
                        f"""UPDATE {table_name}
                            SET call_count = COALESCE(call_count, 0) + 1,
                                updated_at = unixepoch()
                            WHERE {where_sql}""",
                        params,
                    )
                    await db.commit()
                    return result.rowcount == 1
        except Exception as e:
            log.error(f"Error marking credential selected {filename}: {e}")
            return False

    async def get_available_credentials_list(self) -> List[str]:
        """
        获取所有可用凭证列表
        - 未禁用
        - 按轮换顺序排序
        """
        self._ensure_initialized()

        try:
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute("""
                    SELECT filename
                    FROM credentials
                    WHERE disabled = 0
                    ORDER BY rotation_order ASC
                """) as cursor:
                    rows = await cursor.fetchall()
                    return [row[0] for row in rows]

        except Exception as e:
            log.error(f"Error getting available credentials list: {e}")
            return []

    # ============ StorageBackend 协议方法 ============

    async def store_credential(self, filename: str, credential_data: Dict[str, Any], mode: str = "geminicli") -> bool:
        """存储或更新凭证"""
        self._ensure_initialized()

        # 统一使用 basename 处理文件名
        filename = os.path.basename(filename)
        # proxy_mode/proxy_url are columns, never duplicate account proxy secrets
        # inside the credential JSON blob.
        persisted_credential_data = dict(credential_data)
        persisted_credential_data.pop("proxy_mode", None)
        persisted_credential_data.pop("proxy_url", None)
        persisted_credential_data.pop("proxy_group_id", None)
        persisted_credential_data.pop("proxy_node_id", None)
        persisted_credential_data.pop("bound_proxy_node_id", None)

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                # 检查凭证是否存在
                async with db.execute(f"""
                    SELECT disabled, error_codes, last_success, user_email,
                           rotation_order, call_count
                    FROM {table_name} WHERE filename = ?
                """, (filename,)) as cursor:
                    existing = await cursor.fetchone()

                if existing:
                    # 更新现有凭证（保留状态）
                    await db.execute(f"""
                        UPDATE {table_name}
                        SET credential_data = ?,
                            updated_at = unixepoch()
                        WHERE filename = ?
                    """, (json.dumps(persisted_credential_data), filename))
                else:
                    # 插入新凭证
                    async with db.execute(f"""
                        SELECT COALESCE(MAX(rotation_order), -1) + 1 FROM {table_name}
                    """) as cursor:
                        row = await cursor.fetchone()
                        next_order = row[0]

                    initial_call_count = 0
                    if mode == "antigravity":
                        async with db.execute(f"""
                            SELECT COALESCE(MAX(call_count), 0)
                            FROM {table_name}
                        """) as cursor:
                            row = await cursor.fetchone()
                            max_call_count = max(int(row[0] or 0), 0)
                            initial_call_count = max(
                                max_call_count - FAIR_ROUTING_CATCHUP_WINDOW,
                                0,
                            )

                    await db.execute(f"""
                        INSERT INTO {table_name}
                        (filename, credential_data, rotation_order, call_count,
                         last_success)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        filename,
                        json.dumps(persisted_credential_data),
                        next_order,
                        initial_call_count,
                        time.time(),
                    ))

                await db.commit()
                log.debug(f"Stored credential: {filename} (mode={mode})")
                return True

        except Exception as e:
            log.error(f"Error storing credential {filename}: {e}")
            return False

    async def get_credential(self, filename: str, mode: str = "geminicli") -> Optional[Dict[str, Any]]:
        """获取凭证数据"""
        self._ensure_initialized()

        # 统一使用 basename 处理文件名
        filename = os.path.basename(filename)

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                # 精确匹配
                async with db.execute(f"""
                    SELECT credential_data FROM {table_name} WHERE filename = ?
                """, (filename,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return json.loads(row[0])

                return None

        except Exception as e:
            log.error(f"Error getting credential {filename}: {e}")
            return None

    async def get_credential_network(self, filename: str, mode: str = "antigravity") -> Dict[str, Any]:
        """读取账号级代理配置，不返回完整凭证内容。"""
        self._ensure_initialized()
        filename = os.path.basename(filename)
        if mode != "antigravity":
            return {
                "proxy_mode": "inherit",
                "proxy_url": None,
                "proxy_group_id": None,
                "bound_proxy_node_id": None,
            }
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT a.proxy_mode, a.proxy_url, a.proxy_group_id,
                          a.bound_proxy_node_id, n.name, n.proxy_url
                   FROM antigravity_credentials a
                   LEFT JOIN proxy_group_nodes n ON n.id = a.bound_proxy_node_id
                   WHERE a.filename = ?""",
                (filename,),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return {
                "proxy_mode": "inherit",
                "proxy_url": None,
                "proxy_group_id": None,
                "bound_proxy_node_id": None,
            }
        return {
            "proxy_mode": row[0] or "inherit",
            "proxy_url": row[1],
            "proxy_group_id": row[2],
            "bound_proxy_node_id": row[3],
            "bound_proxy_node_name": row[4],
            "bound_proxy_url": row[5],
        }

    async def resolve_credential_network(self, filename: str, mode: str = "antigravity") -> Dict[str, Any]:
        """Resolve and persist the effective account proxy at request time."""
        self._ensure_initialized()
        filename = os.path.basename(filename)
        network = await self.get_credential_network(filename, mode=mode)
        if mode != "antigravity":
            return network
        if network["proxy_mode"] == "group" and network.get("proxy_group_id"):
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    """SELECT proxy_group_id, bound_proxy_node_id
                       FROM antigravity_credentials WHERE filename = ?""",
                    (filename,),
                ) as cursor:
                    binding_row = await cursor.fetchone()
                if not binding_row or not binding_row[0]:
                    await db.rollback()
                    network["proxy_url"] = None
                    return network

                group_id = int(binding_row[0])
                bound_node_id = binding_row[1]
                selected_node = None
                if bound_node_id:
                    current_time = time.time()
                    async with db.execute(
                        """SELECT id, name, proxy_url FROM proxy_group_nodes
                           WHERE id = ? AND group_id = ? AND enabled = 1
                             AND (cooldown_until IS NULL OR cooldown_until <= ?)""",
                        (int(bound_node_id), group_id, current_time),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if row:
                        selected_node = {
                            "id": row[0], "name": row[1], "proxy_url": row[2]
                        }

                if selected_node is None:
                    selected_node = await self._select_proxy_group_node(
                        db, group_id, advance=True, commit=False
                    )
                    await db.execute(
                        """UPDATE antigravity_credentials
                           SET bound_proxy_node_id = ?, updated_at = unixepoch()
                           WHERE filename = ?""",
                        (selected_node["id"] if selected_node else None, filename),
                    )
                await db.commit()
            if selected_node:
                network.update(
                    {
                        "proxy_url": selected_node["proxy_url"],
                        "proxy_node_id": selected_node["id"],
                        "bound_proxy_node_id": selected_node["id"],
                        "proxy_node_name": selected_node["name"],
                    }
                )
            else:
                network["proxy_url"] = None
        return network

    async def get_antigravity_account_health(self, filename: str) -> Dict[str, Any]:
        """Return persisted network/eligibility state for one Antigravity account."""
        self._ensure_initialized()
        filename = os.path.basename(filename)
        columns = (
            "credential_name", "egress_ip", "egress_country", "binding_status",
            "eligibility_status", "eligibility_reason", "eligibility_checked_at",
            "last_403_category", "last_403_reason", "last_403_at", "updated_at",
        )
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                f"SELECT {', '.join(columns)} FROM antigravity_account_health WHERE credential_name = ?",
                (filename,),
            ) as cursor:
                row = await cursor.fetchone()
        if row:
            return dict(zip(columns, row))
        return {
            "credential_name": filename,
            "egress_ip": None,
            "egress_country": None,
            "binding_status": "unchecked",
            "eligibility_status": "unchecked",
            "eligibility_reason": None,
            "eligibility_checked_at": None,
            "last_403_category": None,
            "last_403_reason": None,
            "last_403_at": None,
            "updated_at": None,
        }

    async def update_antigravity_account_health(
        self, filename: str, **updates: Any
    ) -> Dict[str, Any]:
        """Persist selected health fields without storing credentials or proxy secrets."""
        self._ensure_initialized()
        filename = os.path.basename(filename)
        allowed = {
            "egress_ip", "egress_country", "binding_status", "eligibility_status",
            "eligibility_reason", "eligibility_checked_at", "last_403_category",
            "last_403_reason", "last_403_at",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unsupported account health fields: {sorted(unknown)}")
        binding_status = updates.get("binding_status")
        if binding_status not in {None, "unchecked", "healthy", "proxy_drift", "proxy_failed"}:
            raise ValueError("Invalid binding_status")
        eligibility_status = updates.get("eligibility_status")
        if eligibility_status not in {
            None, "unchecked", "eligible", "geo_blocked", "account_blocked",
            "validation_required", "error"
        }:
            raise ValueError("Invalid eligibility_status")

        current = await self.get_antigravity_account_health(filename)
        values = {key: current.get(key) for key in allowed}
        values.update(updates)
        now = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT 1 FROM antigravity_credentials WHERE filename = ?",
                (filename,),
            ) as cursor:
                if not await cursor.fetchone():
                    raise ValueError(f"Antigravity credential not found: {filename}")
            await db.execute(
                """INSERT INTO antigravity_account_health(
                       credential_name, egress_ip, egress_country, binding_status,
                       eligibility_status, eligibility_reason, eligibility_checked_at,
                       last_403_category, last_403_reason, last_403_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(credential_name) DO UPDATE SET
                       egress_ip = excluded.egress_ip,
                       egress_country = excluded.egress_country,
                       binding_status = excluded.binding_status,
                       eligibility_status = excluded.eligibility_status,
                       eligibility_reason = excluded.eligibility_reason,
                       eligibility_checked_at = excluded.eligibility_checked_at,
                       last_403_category = excluded.last_403_category,
                       last_403_reason = excluded.last_403_reason,
                       last_403_at = excluded.last_403_at,
                       updated_at = excluded.updated_at""",
                (
                    filename, values["egress_ip"], values["egress_country"],
                    values["binding_status"], values["eligibility_status"],
                    values["eligibility_reason"], values["eligibility_checked_at"],
                    values["last_403_category"], values["last_403_reason"],
                    values["last_403_at"], now,
                ),
            )
            await db.commit()
        return await self.get_antigravity_account_health(filename)

    async def invalidate_antigravity_proxy_binding(
        self, filename: str, cooldown_seconds: int = 300
    ) -> bool:
        """Release one group binding and briefly cool its failed node."""
        self._ensure_initialized()
        filename = os.path.basename(filename)
        now = time.time()
        cooldown_until = now + max(int(cooldown_seconds), 0)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT proxy_mode, bound_proxy_node_id
                   FROM antigravity_credentials WHERE filename = ?""",
                (filename,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row or row[0] != "group" or not row[1]:
                await db.rollback()
                return False
            node_id = int(row[1])
            await db.execute(
                """UPDATE proxy_group_nodes
                   SET failure_count = failure_count + 1,
                       cooldown_until = ?, updated_at = ?
                   WHERE id = ?""",
                (cooldown_until, now, node_id),
            )
            await db.execute(
                """UPDATE antigravity_credentials
                   SET bound_proxy_node_id = NULL, updated_at = ?
                   WHERE filename = ?""",
                (now, filename),
            )
            await db.execute(
                """UPDATE antigravity_account_health
                   SET egress_ip = NULL, egress_country = NULL,
                       binding_status = 'unchecked',
                       eligibility_status = 'unchecked',
                       eligibility_reason = NULL,
                       eligibility_checked_at = NULL,
                       updated_at = ?
                   WHERE credential_name = ?""",
                (now, filename),
            )
            await db.commit()
        return True

    async def create_proxy_group(
        self,
        name: str,
        description: str = "",
        strategy: str = "round_robin",
        enabled: bool = True,
    ) -> Dict[str, Any]:
        """Create a proxy group and return its public shape."""
        self._ensure_initialized()
        name = str(name or "").strip()
        if not name:
            raise ValueError("代理组名称不能为空")
        if strategy not in {"round_robin", "random", "failover"}:
            raise ValueError("代理组策略必须是 round_robin、random 或 failover")
        async with aiosqlite.connect(self._db_path) as db:
            try:
                cursor = await db.execute(
                    """INSERT INTO proxy_groups(name, description, strategy, enabled)
                       VALUES (?, ?, ?, ?)""",
                    (name, str(description or "").strip(), strategy, int(bool(enabled))),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ValueError("代理组名称已存在") from exc
                raise
            group_id = cursor.lastrowid
            await db.commit()
        return await self.get_proxy_group(int(group_id))

    async def get_proxy_group(self, group_id: int) -> Dict[str, Any]:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT id, name, description, strategy, enabled, cursor,
                          created_at, updated_at
                   FROM proxy_groups WHERE id = ?""",
                (int(group_id),),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                raise ValueError("代理组不存在")
            async with db.execute(
                """SELECT id, name, proxy_url, enabled, failure_count,
                          cooldown_until, last_used_at, created_at, updated_at
                   FROM proxy_group_nodes WHERE group_id = ? ORDER BY id""",
                (int(group_id),),
            ) as cursor:
                nodes = await cursor.fetchall()
            async with db.execute(
                "SELECT COUNT(*) FROM antigravity_credentials WHERE proxy_mode = 'group' AND proxy_group_id = ?",
                (int(group_id),),
            ) as cursor:
                assigned_count = (await cursor.fetchone())[0]
        node_count = len(nodes)
        enabled_node_count = sum(1 for node in nodes if node[3])
        return {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "strategy": row[3],
            "enabled": bool(row[4]),
            "cursor": row[5],
            "created_at": row[6],
            "updated_at": row[7],
            "assigned_account_count": assigned_count,
            "node_count": node_count,
            "enabled_node_count": enabled_node_count,
            "nodes": [
                {
                    "id": node[0],
                    "name": node[1],
                    "proxy_url": node[2],
                    "enabled": bool(node[3]),
                    "failure_count": node[4],
                    "cooldown_until": node[5],
                    "last_used_at": node[6],
                    "created_at": node[7],
                    "updated_at": node[8],
                }
                for node in nodes
            ],
        }

    async def list_proxy_groups(self) -> List[Dict[str, Any]]:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT g.id, g.name, g.description, g.strategy, g.enabled,
                          g.cursor, g.created_at, g.updated_at,
                          COUNT(n.id) AS node_count,
                          COALESCE(SUM(CASE WHEN n.enabled = 1 THEN 1 ELSE 0 END), 0) AS enabled_node_count,
                          (SELECT COUNT(*) FROM antigravity_credentials a
                           WHERE a.proxy_mode = 'group' AND a.proxy_group_id = g.id) AS assigned_account_count
                   FROM proxy_groups g
                   LEFT JOIN proxy_group_nodes n ON n.group_id = g.id
                   GROUP BY g.id
                   ORDER BY g.name, g.id
                   LIMIT 100"""
            ) as cursor:
                rows = await cursor.fetchall()
            groups: List[Dict[str, Any]] = []
            for row in rows:
                async with db.execute(
                    """SELECT id, name, proxy_url, enabled, failure_count,
                              cooldown_until, last_used_at, created_at, updated_at
                       FROM proxy_group_nodes WHERE group_id = ? ORDER BY id LIMIT 3""",
                    (row[0],),
                ) as cursor:
                    node_rows = await cursor.fetchall()
                groups.append(
                    {
                        "id": row[0],
                        "name": row[1],
                        "description": row[2],
                        "strategy": row[3],
                        "enabled": bool(row[4]),
                        "cursor": row[5],
                        "created_at": row[6],
                        "updated_at": row[7],
                        "node_count": row[8],
                        "enabled_node_count": row[9],
                        "assigned_account_count": row[10],
                        "nodes": [
                            {
                                "id": node[0],
                                "name": node[1],
                                "proxy_url": node[2],
                                "enabled": bool(node[3]),
                                "failure_count": node[4],
                                "cooldown_until": node[5],
                                "last_used_at": node[6],
                                "created_at": node[7],
                                "updated_at": node[8],
                            }
                            for node in node_rows
                        ],
                    }
                )
        return groups

    async def update_proxy_group(self, group_id: int, **updates: Any) -> Dict[str, Any]:
        self._ensure_initialized()
        allowed = {"name", "description", "strategy", "enabled"}
        values = {key: value for key, value in updates.items() if key in allowed and value is not None}
        if not values:
            return await self.get_proxy_group(group_id)
        if "name" in values:
            values["name"] = str(values["name"]).strip()
            if not values["name"]:
                raise ValueError("代理组名称不能为空")
        if "strategy" in values and values["strategy"] not in {"round_robin", "random", "failover"}:
            raise ValueError("代理组策略必须是 round_robin、random 或 failover")
        if "enabled" in values:
            values["enabled"] = int(bool(values["enabled"]))
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = list(values.values()) + [int(group_id)]
        async with aiosqlite.connect(self._db_path) as db:
            try:
                result = await db.execute(
                    f"UPDATE proxy_groups SET {assignments} WHERE id = ?", params
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise ValueError("代理组名称已存在") from exc
                raise
            if result.rowcount == 0:
                raise ValueError("代理组不存在")
            await db.commit()
        return await self.get_proxy_group(group_id)

    async def delete_proxy_group(self, group_id: int) -> bool:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM antigravity_credentials WHERE proxy_mode = 'group' AND proxy_group_id = ?",
                (int(group_id),),
            ) as cursor:
                assigned_count = (await cursor.fetchone())[0]
            if assigned_count:
                raise ValueError("代理组仍被 Antigravity 账号使用，请先批量切换账号代理")
            # foreign_keys is a per-connection SQLite setting. Delete nodes
            # explicitly so this remains correct even on connections where the
            # pragma was not enabled.
            await db.execute(
                "DELETE FROM proxy_group_nodes WHERE group_id = ?", (int(group_id),)
            )
            result = await db.execute("DELETE FROM proxy_groups WHERE id = ?", (int(group_id),))
            await db.commit()
        return result.rowcount > 0

    async def import_proxy_group_nodes(
        self, group_id: int, nodes: List[Dict[str, str]], replace: bool = False
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        if not nodes:
            raise ValueError("至少提供一个代理节点")
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute("SELECT id FROM proxy_groups WHERE id = ?", (int(group_id),)) as cursor:
                if not await cursor.fetchone():
                    raise ValueError("代理组不存在")
            if replace:
                await db.execute("DELETE FROM proxy_group_nodes WHERE group_id = ?", (int(group_id),))
            imported_count = 0
            updated_count = 0
            for node in nodes:
                proxy_url = node["proxy_url"]
                name = str(node.get("name") or "").strip()
                async with db.execute(
                    "SELECT id FROM proxy_group_nodes WHERE group_id = ? AND proxy_url = ?",
                    (int(group_id), proxy_url),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing:
                    await db.execute(
                        """UPDATE proxy_group_nodes SET name = ?, enabled = 1,
                           updated_at = unixepoch() WHERE id = ?""",
                        (name, existing[0]),
                    )
                    updated_count += 1
                else:
                    await db.execute(
                        """INSERT INTO proxy_group_nodes(group_id, name, proxy_url)
                           VALUES (?, ?, ?)""",
                        (int(group_id), name, proxy_url),
                    )
                    imported_count += 1
            await db.execute("UPDATE proxy_groups SET updated_at = unixepoch() WHERE id = ?", (int(group_id),))
            await db.commit()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM proxy_group_nodes WHERE group_id = ?", (int(group_id),)
            ) as cursor:
                node_count = (await cursor.fetchone())[0]
        return {
            "group_id": int(group_id),
            "imported_count": imported_count,
            "updated_count": updated_count,
            "node_count": node_count,
        }

    async def select_proxy_group_node(
        self, group_id: int, advance: bool = True
    ) -> Optional[Dict[str, Any]]:
        """按组的切换策略挑选一个可用节点（供导入等无账号场景使用）。"""
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            return await self._select_proxy_group_node(
                db, int(group_id), advance=advance, commit=True
            )

    async def _select_proxy_group_node(
        self, db: aiosqlite.Connection, group_id: int, advance: bool = True,
        commit: bool = True,
    ) -> Optional[Dict[str, Any]]:
        current_time = time.time()
        async with db.execute(
            "SELECT strategy, enabled, cursor FROM proxy_groups WHERE id = ?",
            (int(group_id),),
        ) as cursor:
            group = await cursor.fetchone()
        if not group or not group[1]:
            return None
        strategy, group_cursor = group[0], group[2]
        async with db.execute(
            """SELECT id, name, proxy_url FROM proxy_group_nodes
               WHERE group_id = ? AND enabled = 1
                 AND (cooldown_until IS NULL OR cooldown_until <= ?)
               ORDER BY id""",
            (int(group_id), current_time),
        ) as cursor:
            nodes = await cursor.fetchall()
        if not nodes:
            return None
        if strategy == "random":
            async with db.execute(
                """SELECT id, name, proxy_url FROM proxy_group_nodes
                   WHERE group_id = ? AND enabled = 1
                     AND (cooldown_until IS NULL OR cooldown_until <= ?)
                   ORDER BY RANDOM() LIMIT 1""",
                (int(group_id), current_time),
            ) as cursor:
                selected = await cursor.fetchone()
        elif strategy == "failover":
            selected = nodes[0]
        else:
            selected = nodes[int(group_cursor or 0) % len(nodes)]
        if not selected:
            return None
        if advance and strategy == "round_robin":
            await db.execute(
                "UPDATE proxy_groups SET cursor = ?, updated_at = unixepoch() WHERE id = ?",
                ((int(group_cursor or 0) + 1) % len(nodes), int(group_id)),
            )
        if advance:
            await db.execute(
                "UPDATE proxy_group_nodes SET last_used_at = ?, updated_at = unixepoch() WHERE id = ?",
                (current_time, selected[0]),
            )
        if advance and commit:
            await db.commit()
        return {"id": selected[0], "name": selected[1], "proxy_url": selected[2]}

    async def update_credential_network(
        self, filename: str, proxy_mode: str = "inherit", proxy_url: Optional[str] = None,
        mode: str = "antigravity", proxy_group_id: Optional[int] = None,
    ) -> bool:
        """更新账号级代理配置。"""
        self._ensure_initialized()
        if mode != "antigravity":
            raise ValueError("账号代理只适用于 antigravity")
        if proxy_mode not in {"inherit", "custom", "direct", "group"}:
            raise ValueError("proxy_mode 必须是 inherit、custom、direct 或 group")
        if proxy_mode == "custom" and not proxy_url:
            raise ValueError("custom 代理模式必须提供 proxy_url")
        if proxy_mode == "group":
            if not proxy_group_id:
                raise ValueError("group 代理模式必须提供 proxy_group_id")
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute("SELECT id FROM proxy_groups WHERE id = ?", (int(proxy_group_id),)) as cursor:
                    if not await cursor.fetchone():
                        raise ValueError("代理组不存在")
        if proxy_mode != "custom":
            proxy_url = None
        if proxy_mode != "group":
            proxy_group_id = None
        filename = os.path.basename(filename)
        async with aiosqlite.connect(self._db_path) as db:
            result = await db.execute(
                """UPDATE antigravity_credentials
                   SET proxy_mode = ?, proxy_url = ?, proxy_group_id = ?,
                       bound_proxy_node_id = NULL, updated_at = unixepoch()
                   WHERE filename = ?""",
                (proxy_mode, proxy_url, proxy_group_id, filename),
            )
            await db.execute(
                """UPDATE antigravity_account_health
                   SET egress_ip = NULL, egress_country = NULL,
                       binding_status = 'unchecked', eligibility_status = 'unchecked',
                       eligibility_reason = NULL, eligibility_checked_at = NULL,
                       updated_at = unixepoch()
                   WHERE credential_name = ?""",
                (filename,),
            )
            await db.commit()
        return result.rowcount > 0

    async def batch_update_credential_network(
        self,
        filenames: List[str],
        proxy_mode: str = "inherit",
        proxy_url: Optional[str] = None,
        proxy_group_id: Optional[int] = None,
        mode: str = "antigravity",
    ) -> Dict[str, Any]:
        """Atomically assign one network policy to multiple Antigravity accounts."""
        self._ensure_initialized()
        if mode != "antigravity":
            raise ValueError("账号代理只适用于 antigravity")
        names = list(dict.fromkeys(os.path.basename(str(name)) for name in filenames if str(name).strip()))
        if not names:
            raise ValueError("至少选择一个凭证")
        if proxy_mode not in {"inherit", "custom", "direct", "group"}:
            raise ValueError("proxy_mode 必须是 inherit、custom、direct 或 group")
        if proxy_mode == "custom" and not proxy_url:
            raise ValueError("custom 代理模式必须提供 proxy_url")
        if proxy_mode == "group" and not proxy_group_id:
            raise ValueError("group 代理模式必须提供 proxy_group_id")
        if proxy_mode != "custom":
            proxy_url = None
        if proxy_mode != "group":
            proxy_group_id = None
        async with aiosqlite.connect(self._db_path) as db:
            if proxy_group_id:
                async with db.execute("SELECT id FROM proxy_groups WHERE id = ?", (int(proxy_group_id),)) as cursor:
                    if not await cursor.fetchone():
                        raise ValueError("代理组不存在")
            # Stay below conservative SQLite bind-variable limits while keeping
            # validation and updates inside one transaction.
            chunks = [names[index:index + 400] for index in range(0, len(names), 400)]
            found = set()
            for chunk in chunks:
                placeholders = ",".join("?" for _ in chunk)
                async with db.execute(
                    f"SELECT filename FROM antigravity_credentials WHERE filename IN ({placeholders})",
                    chunk,
                ) as cursor:
                    found.update(row[0] for row in await cursor.fetchall())
            missing = [name for name in names if name not in found]
            if missing:
                return {"updated_count": 0, "total_count": len(names), "missing": missing}
            for chunk in chunks:
                placeholders = ",".join("?" for _ in chunk)
                await db.execute(
                    f"""UPDATE antigravity_credentials
                        SET proxy_mode = ?, proxy_url = ?, proxy_group_id = ?,
                            bound_proxy_node_id = NULL, updated_at = unixepoch()
                        WHERE filename IN ({placeholders})""",
                    [proxy_mode, proxy_url, proxy_group_id, *chunk],
                )
                await db.execute(
                    f"""UPDATE antigravity_account_health
                        SET egress_ip = NULL, egress_country = NULL,
                            binding_status = 'unchecked', eligibility_status = 'unchecked',
                            eligibility_reason = NULL, eligibility_checked_at = NULL,
                            updated_at = unixepoch()
                        WHERE credential_name IN ({placeholders})""",
                    chunk,
                )
            await db.commit()
        return {"updated_count": len(names), "total_count": len(names), "missing": []}

    # ============ 下游 API 密钥管理 ============

    API_KEY_COLUMNS = (
        "id", "name", "key_prefix", "secret_hash", "secret_ciphertext",
        "kind", "status",
        "quota_amount", "quota_used", "quota_tokens_used", "currency", "reset_mode",
        "quota_period_start", "expires_at", "last_used_at", "last_reset_at",
        "revoked_at", "created_at", "updated_at",
    )

    @classmethod
    def _api_key_record(cls, row: Tuple[Any, ...]) -> Dict[str, Any]:
        return dict(zip(cls.API_KEY_COLUMNS, row))

    async def create_api_key_record(self, **values: Any) -> Dict[str, Any]:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO api_keys(
                    id, name, key_prefix, secret_hash, secret_ciphertext, kind, status,
                    quota_amount, quota_used, quota_tokens_used, currency, reset_mode,
                    quota_period_start, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'managed', 'active', ?, '0.00000000', 0, ?, ?, ?, ?, ?, ?)""",
                (
                    values["id"], values["name"], values["key_prefix"],
                    values["secret_hash"], values["secret_ciphertext"],
                    values.get("quota_amount"),
                    values["currency"], values["reset_mode"],
                    values["quota_period_start"], values.get("expires_at"),
                    values["now"], values["now"],
                ),
            )
            await db.commit()
        record = await self.get_api_key_record(values["id"], include_secret=True)
        if record is None:
            raise RuntimeError("密钥创建后读取失败")
        return record

    async def get_api_key_record(
        self, api_key_id: str, *, include_secret: bool = False
    ) -> Optional[Dict[str, Any]]:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                f"SELECT {', '.join(self.API_KEY_COLUMNS)} FROM api_keys WHERE id = ?",
                (api_key_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        result = self._api_key_record(row)
        if not include_secret:
            result.pop("secret_hash", None)
            result["secret_available"] = bool(
                result.pop("secret_ciphertext", None)
            )
        return result

    async def get_api_key_by_prefix(self, key_prefix: str) -> Optional[Dict[str, Any]]:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                f"SELECT {', '.join(self.API_KEY_COLUMNS)} FROM api_keys WHERE key_prefix = ? AND kind = 'managed'",
                (key_prefix,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        result = self._api_key_record(row)
        # Request authentication needs the one-way hash, never the retrievable
        # ciphertext. Keep the encrypted secret out of this hot path.
        result.pop("secret_ciphertext", None)
        return result

    async def list_api_key_records(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 100)
        offset = (page - 1) * page_size
        where = ""
        params: List[Any] = []
        if status:
            where = " WHERE status = ?"
            params.append(status)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(f"SELECT COUNT(*) FROM api_keys{where}", params) as cursor:
                total = int((await cursor.fetchone())[0])
            async with db.execute(
                f"""SELECT {', '.join(self.API_KEY_COLUMNS)} FROM api_keys{where}
                    ORDER BY CASE kind WHEN 'environment' THEN 0 ELSE 1 END,
                             created_at DESC, name COLLATE NOCASE ASC
                    LIMIT ? OFFSET ?""",
                [*params, page_size, offset],
            ) as cursor:
                rows = await cursor.fetchall()
        items = []
        for row in rows:
            item = self._api_key_record(row)
            item.pop("secret_hash", None)
            item["secret_available"] = bool(
                item.pop("secret_ciphertext", None)
            )
            items.append(item)
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": offset + page_size < total,
        }

    async def update_api_key_record(self, api_key_id: str, **updates: Any) -> Dict[str, Any]:
        self._ensure_initialized()
        allowed = {
            "name", "status", "quota_amount", "currency", "reset_mode",
            "quota_period_start", "expires_at",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"不支持的密钥字段: {', '.join(sorted(unknown))}")
        if not updates:
            record = await self.get_api_key_record(api_key_id)
            if record is None:
                raise KeyError(api_key_id)
            return record
        assignments = [f"{field} = ?" for field in updates]
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT kind, status FROM api_keys WHERE id = ?", (api_key_id,)
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is None:
                raise KeyError(api_key_id)
            if existing[0] == "environment":
                raise ValueError("环境配置密钥为只读")
            if existing[1] == "revoked":
                raise ValueError("已吊销密钥不能修改")
            result = await db.execute(
                f"UPDATE api_keys SET {', '.join(assignments)}, updated_at = unixepoch() WHERE id = ?",
                [*updates.values(), api_key_id],
            )
            if result.rowcount == 0:
                raise KeyError(api_key_id)
            await db.commit()
        record = await self.get_api_key_record(api_key_id)
        if record is None:
            raise KeyError(api_key_id)
        return record

    async def revoke_api_key(self, api_key_id: str) -> bool:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT kind FROM api_keys WHERE id = ?", (api_key_id,)
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is None:
                return False
            if existing[0] == "environment":
                raise ValueError("环境配置密钥为只读")
            await db.execute(
                """UPDATE api_keys
                   SET status = 'revoked', revoked_at = COALESCE(revoked_at, unixepoch()),
                       updated_at = unixepoch()
                   WHERE id = ?""",
                (api_key_id,),
            )
            await db.commit()
        return True

    async def rollover_api_key_quota(
        self, api_key_id: str, *, month_start: str, now: float
    ) -> bool:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT kind, reset_mode, quota_period_start, quota_used
                   FROM api_keys WHERE id = ?""",
                (api_key_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.commit()
                return False
            should_reset = (
                row[0] == "managed"
                and row[1] == "monthly"
                and row[2] != month_start
            )
            if should_reset:
                before = f"{Decimal(str(row[3] or '0')).quantize(Decimal('0.00000001')):.8f}"
                await db.execute(
                    """INSERT INTO api_key_quota_events(
                           api_key_id, event_type, amount_before, amount_after, created_at
                       ) VALUES (?, 'monthly_reset', ?, '0.00000000', ?)""",
                    (api_key_id, before, now),
                )
                await db.execute(
                    """UPDATE api_keys SET quota_used = '0.00000000', quota_tokens_used = 0,
                           quota_period_start = ?, last_reset_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (month_start, now, now, api_key_id),
                )
            await db.commit()
        return should_reset

    async def rollover_all_api_key_quotas(self, *, month_start: str, now: float) -> int:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT id, quota_used FROM api_keys
                   WHERE kind = 'managed' AND reset_mode = 'monthly'
                     AND quota_period_start != ?""",
                (month_start,),
            ) as cursor:
                rows = await cursor.fetchall()
            for api_key_id, quota_used in rows:
                before = f"{Decimal(str(quota_used or '0')).quantize(Decimal('0.00000001')):.8f}"
                await db.execute(
                    """INSERT INTO api_key_quota_events(
                           api_key_id, event_type, amount_before, amount_after, created_at
                       ) VALUES (?, 'monthly_reset', ?, '0.00000000', ?)""",
                    (api_key_id, before, now),
                )
            if rows:
                await db.execute(
                    """UPDATE api_keys SET quota_used = '0.00000000', quota_tokens_used = 0,
                           quota_period_start = ?, last_reset_at = ?, updated_at = ?
                       WHERE kind = 'managed' AND reset_mode = 'monthly'
                         AND quota_period_start != ?""",
                    (month_start, now, now, month_start),
                )
            await db.commit()
        return len(rows)

    async def reset_api_key_quota(self, api_key_id: str, *, period_start: str, now: float) -> Dict[str, Any]:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT kind, status, reset_mode, quota_used
                   FROM api_keys WHERE id = ?""",
                (api_key_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise KeyError(api_key_id)
            if row[0] == "environment":
                await db.rollback()
                raise ValueError("环境配置密钥为只读")
            if row[1] == "revoked":
                await db.rollback()
                raise ValueError("已吊销密钥不能重置")
            if row[2] != "manual":
                await db.rollback()
                raise ValueError("只有手动重置模式可以使用重置按钮")
            before = f"{Decimal(str(row[3] or '0')).quantize(Decimal('0.00000001')):.8f}"
            await db.execute(
                """INSERT INTO api_key_quota_events(
                       api_key_id, event_type, amount_before, amount_after, created_at
                   ) VALUES (?, 'manual_reset', ?, '0.00000000', ?)""",
                (api_key_id, before, now),
            )
            await db.execute(
                """UPDATE api_keys SET quota_used = '0.00000000', quota_tokens_used = 0,
                       quota_period_start = ?, last_reset_at = ?, updated_at = ?
                   WHERE id = ?""",
                (period_start, now, now, api_key_id),
            )
            await db.commit()
        record = await self.get_api_key_record(api_key_id)
        if record is None:
            raise KeyError(api_key_id)
        return record

    async def set_api_key_quota_used(self, api_key_id: str, amount: str) -> None:
        """Set the exact quota counter; used by migrations, tests and repair tooling."""
        self._ensure_initialized()
        normalized = f"{Decimal(str(amount)).quantize(Decimal('0.00000001')):.8f}"
        async with aiosqlite.connect(self._db_path) as db:
            result = await db.execute(
                "UPDATE api_keys SET quota_used = ?, updated_at = unixepoch() WHERE id = ?",
                (normalized, api_key_id),
            )
            if result.rowcount == 0:
                raise KeyError(api_key_id)
            await db.commit()

    async def set_api_key_quota_tokens_used(self, api_key_id: str, tokens: int) -> None:
        """Set the exact token counter; used by tests and repair tooling."""
        self._ensure_initialized()
        normalized = max(int(tokens), 0)
        async with aiosqlite.connect(self._db_path) as db:
            result = await db.execute(
                "UPDATE api_keys SET quota_tokens_used = ?, updated_at = unixepoch() WHERE id = ?",
                (normalized, api_key_id),
            )
            if result.rowcount == 0:
                raise KeyError(api_key_id)
            await db.commit()

    async def record_billing_usage(
        self,
        *,
        request_id: str,
        billing_date: str,
        credential_name: str,
        model: str,
        currency: str,
        input_tokens: int,
        output_tokens: int,
        cache_tokens: int,
        thought_tokens: int,
        total_tokens: int,
        input_cost: str,
        output_cost: str,
        cache_cost: str,
        total_cost: str,
        success: bool,
        unknown_usage: bool,
        dedupe_ttl_seconds: int,
        api_key_id: str = "env",
    ) -> bool:
        """在一个事务中去重并更新每日累计；返回是否首次记录。"""
        self._ensure_initialized()
        now = time.time()
        expires_at = now + max(int(dedupe_ttl_seconds), 1)
        incoming_costs = tuple(
            f"{Decimal(str(value or '0')).quantize(Decimal('0.00000001')):.8f}"
            for value in (input_cost, output_cost, cache_cost, total_cost)
        )
        daily_key = (billing_date, api_key_id, os.path.basename(credential_name), model, currency)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "DELETE FROM billing_request_dedupe WHERE request_id IN (SELECT request_id FROM billing_request_dedupe WHERE expires_at < ? LIMIT 500)",
                (now,),
            )
            # A request whose own TTL elapsed must be eligible immediately even
            # when the bounded batch above has other expired rows to remove.
            await db.execute(
                "DELETE FROM billing_request_dedupe WHERE request_id = ? AND expires_at < ?",
                (request_id, now),
            )
            inserted = await db.execute(
                "INSERT OR IGNORE INTO billing_request_dedupe(request_id, created_at, expires_at) VALUES (?, ?, ?)",
                (request_id, now, expires_at),
            )
            if inserted.rowcount == 0:
                await db.commit()
                return False
            async with db.execute(
                """SELECT input_cost, output_cost, cache_cost, total_cost
                   FROM billing_daily
                   WHERE billing_date = ? AND api_key_id = ? AND credential_name = ? AND model = ? AND currency = ?""",
                daily_key,
            ) as cursor:
                existing_costs = await cursor.fetchone()
            if existing_costs:
                accumulated_costs = tuple(
                    f"{(Decimal(str(current)) + Decimal(incoming)).quantize(Decimal('0.00000001')):.8f}"
                    for current, incoming in zip(existing_costs, incoming_costs)
                )
                await db.execute(
                    """UPDATE billing_daily SET
                        input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,
                        cache_tokens = cache_tokens + ?, thought_tokens = thought_tokens + ?,
                        total_tokens = total_tokens + ?, input_cost = ?, output_cost = ?,
                        cache_cost = ?, total_cost = ?, success_count = success_count + ?,
                        failed_count = failed_count + ?, unknown_usage_count = unknown_usage_count + ?,
                        updated_at = unixepoch()
                       WHERE billing_date = ? AND api_key_id = ? AND credential_name = ? AND model = ? AND currency = ?""",
                    (
                        max(int(input_tokens), 0), max(int(output_tokens), 0), max(int(cache_tokens), 0),
                        max(int(thought_tokens), 0), max(int(total_tokens), 0), *accumulated_costs,
                        1 if success else 0, 0 if success else 1, 1 if unknown_usage else 0, *daily_key,
                    ),
                )
            else:
                await db.execute(
                    """INSERT INTO billing_daily(
                        billing_date, api_key_id, credential_name, model, currency,
                        input_tokens, output_tokens, cache_tokens, thought_tokens, total_tokens,
                        input_cost, output_cost, cache_cost, total_cost,
                        success_count, failed_count, unknown_usage_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())""",
                    (
                        *daily_key, max(int(input_tokens), 0), max(int(output_tokens), 0),
                        max(int(cache_tokens), 0), max(int(thought_tokens), 0), max(int(total_tokens), 0),
                        *incoming_costs, 1 if success else 0, 0 if success else 1,
                        1 if unknown_usage else 0,
                    ),
                )
            async with db.execute(
                """SELECT kind, reset_mode, quota_period_start, quota_used, quota_tokens_used
                   FROM api_keys WHERE id = ?""",
                (api_key_id,),
            ) as cursor:
                key_row = await cursor.fetchone()
            if key_row is not None:
                month_start = f"{billing_date[:7]}-01"
                current_used = Decimal(str(key_row[3] or "0"))
                current_tokens_used = max(int(key_row[4] or 0), 0)
                if key_row[0] == "managed" and key_row[1] == "monthly" and key_row[2] != month_start:
                    await db.execute(
                        """INSERT INTO api_key_quota_events(
                               api_key_id, event_type, amount_before, amount_after, created_at
                           ) VALUES (?, 'monthly_reset', ?, '0.00000000', ?)""",
                        (api_key_id, f"{current_used.quantize(Decimal('0.00000001')):.8f}", now),
                    )
                    current_used = Decimal("0")
                    current_tokens_used = 0
                    await db.execute(
                        """UPDATE api_keys SET quota_used = '0.00000000', quota_tokens_used = 0,
                               quota_period_start = ?, last_reset_at = ?, updated_at = ?
                           WHERE id = ?""",
                        (month_start, now, now, api_key_id),
                    )
                charged = (current_used + Decimal(incoming_costs[3])).quantize(Decimal("0.00000001"))
                charged_tokens = current_tokens_used + max(int(total_tokens), 0)
                await db.execute(
                    """UPDATE api_keys SET quota_used = ?, quota_tokens_used = ?,
                           last_used_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (f"{charged:.8f}", charged_tokens, now, now, api_key_id),
                )
            await db.commit()
        return True

    async def upsert_billing_price(
        self, model: str, input_price: str, output_price: str, cache_price: str, currency: str
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """INSERT INTO billing_prices(model, input_price, output_price, cache_price, currency, updated_at)
                   VALUES (?, ?, ?, ?, ?, unixepoch())
                   ON CONFLICT(model) DO UPDATE SET input_price=excluded.input_price,
                       output_price=excluded.output_price, cache_price=excluded.cache_price,
                       currency=excluded.currency, updated_at=unixepoch()""",
                (model, input_price, output_price, cache_price, currency),
            )
            await db.commit()
        return {"model": model, "input_price": input_price, "output_price": output_price,
                "cache_price": cache_price, "currency": currency}

    async def list_billing_prices(self) -> List[Dict[str, Any]]:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT model, input_price, output_price, cache_price, currency, updated_at FROM billing_prices ORDER BY model"
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {"model": row[0], "input_price": row[1], "output_price": row[2], "cache_price": row[3],
             "currency": row[4], "updated_at": row[5]}
            for row in rows
        ]

    async def delete_billing_price(self, model: str) -> bool:
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            result = await db.execute("DELETE FROM billing_prices WHERE model = ? AND model != 'default'", (model,))
            await db.commit()
        return result.rowcount > 0

    async def list_billing_daily_for_reprice(
        self, model: Optional[str], page: int = 1, page_size: int = 500
    ) -> List[Dict[str, Any]]:
        """分页返回需要按新价格重算的历史汇总行。

        model 为具体模型时只取该模型；model 为 None 表示默认价格变更，
        取所有没有独立价格行的模型（它们实际使用 default 价格）。
        """
        self._ensure_initialized()
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 500)
        offset = (page - 1) * page_size
        if model is None:
            where = "model NOT IN (SELECT model FROM billing_prices WHERE model != 'default')"
            params = (page_size, offset)
        else:
            where = "model = ?"
            params = (model, page_size, offset)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                f"""SELECT billing_date, api_key_id, credential_name, model, currency,
                          input_tokens, output_tokens, cache_tokens, thought_tokens, total_tokens
                   FROM billing_daily WHERE {where}
                   ORDER BY billing_date, api_key_id, credential_name, model, currency
                   LIMIT ? OFFSET ?""",
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        keys = ("billing_date", "api_key_id", "credential_name", "model", "currency",
                "input_tokens", "output_tokens", "cache_tokens", "thought_tokens", "total_tokens")
        return [dict(zip(keys, row)) for row in rows]

    async def update_billing_daily_costs(
        self,
        *,
        billing_date: str,
        api_key_id: str,
        credential_name: str,
        model: str,
        currency: str,
        input_cost: str,
        output_cost: str,
        cache_cost: str,
        total_cost: str,
    ) -> bool:
        """按主键重写一行的成本列（价格变更后的历史重算）。"""
        self._ensure_initialized()
        async with aiosqlite.connect(self._db_path) as db:
            result = await db.execute(
                """UPDATE billing_daily SET input_cost = ?, output_cost = ?,
                       cache_cost = ?, total_cost = ?, updated_at = unixepoch()
                   WHERE billing_date = ? AND api_key_id = ? AND credential_name = ?
                     AND model = ? AND currency = ?""",
                (
                    input_cost, output_cost, cache_cost, total_cost,
                    billing_date, api_key_id, credential_name, model, currency,
                ),
            )
            await db.commit()
        return result.rowcount > 0

    async def rebuild_api_key_quotas(self) -> int:
        """从 billing_daily 重建每个密钥当前配额周期的已用金额。

        价格重算会改变历史成本，api_keys.quota_used 需要同步重建，
        逻辑与配额计数列的首次回填保持一致（Python 侧 Decimal 求和）。
        """
        self._ensure_initialized()
        usage_by_key: Dict[str, Decimal] = {}
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT api_keys.id, billing_daily.total_cost
                   FROM api_keys
                   JOIN billing_daily ON billing_daily.api_key_id = api_keys.id
                   WHERE billing_daily.billing_date >= api_keys.quota_period_start"""
            ) as cursor:
                async for api_key_id, total_cost in cursor:
                    usage_by_key[api_key_id] = (
                        usage_by_key.get(api_key_id, Decimal("0"))
                        + Decimal(str(total_cost or "0"))
                    )
            async with db.execute("SELECT id FROM api_keys") as cursor:
                all_key_ids = [row[0] for row in await cursor.fetchall()]
            for api_key_id in all_key_ids:
                total = usage_by_key.get(api_key_id, Decimal("0"))
                await db.execute(
                    "UPDATE api_keys SET quota_used = ? WHERE id = ?",
                    (f"{total.quantize(Decimal('0.00000001')):.8f}", api_key_id),
                )
            await db.commit()
        return len(all_key_ids)

    @staticmethod
    def _billing_range_dates(range_name: str, today: Optional[date] = None) -> List[str]:
        if range_name not in {"today", "7d", "14d", "30d"}:
            raise ValueError("range 必须是 today、7d、14d 或 30d")
        days = {"today": 1, "7d": 7, "14d": 14, "30d": 30}[range_name]
        today = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
        return [(today - timedelta(days=offset)).isoformat() for offset in range(days)]

    async def get_billing_summary(
        self, range_name: str = "today", api_key_id: Optional[str] = None
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        dates = self._billing_range_dates(range_name)
        start_date, end_date = dates[-1], dates[0]
        key_filter = " AND api_key_id = ?" if api_key_id else ""
        params: Tuple[Any, ...] = (start_date, end_date, api_key_id) if api_key_id else (start_date, end_date)
        async with aiosqlite.connect(self._db_path) as db:
            await self._register_money_functions(db)
            async with db.execute(
                """SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                          COALESCE(SUM(cache_tokens), 0), COALESCE(SUM(thought_tokens), 0),
                          COALESCE(SUM(total_tokens), 0),
                          COALESCE(SUM(money_e8(input_cost)), 0),
                          COALESCE(SUM(money_e8(output_cost)), 0),
                          COALESCE(SUM(money_e8(cache_cost)), 0),
                          COALESCE(SUM(money_e8(total_cost)), 0),
                          COALESCE(SUM(success_count), 0), COALESCE(SUM(failed_count), 0),
                          COALESCE(SUM(unknown_usage_count), 0)
                   FROM billing_daily WHERE billing_date BETWEEN ? AND ?""" + key_filter,
                params,
            ) as cursor:
                aggregate = await cursor.fetchone()
            async with db.execute(
                """SELECT billing_date, COALESCE(SUM(total_tokens), 0),
                          COALESCE(SUM(money_e8(total_cost)), 0)
                   FROM billing_daily WHERE billing_date BETWEEN ? AND ?""" + key_filter + """
                   GROUP BY billing_date ORDER BY billing_date ASC LIMIT 30""",
                params,
            ) as cursor:
                trend_rows = await cursor.fetchall()
        costs = [Decimal(int(aggregate[idx] or 0)) / Decimal(100_000_000) for idx in range(5, 9)]
        return {
            "range": range_name, "timezone": "Asia/Shanghai", "dates": dates,
            "input_tokens": int(aggregate[0]), "output_tokens": int(aggregate[1]), "cache_tokens": int(aggregate[2]),
            "thought_tokens": int(aggregate[3]), "total_tokens": int(aggregate[4]),
            "input_cost": f"{costs[0]:.8f}", "output_cost": f"{costs[1]:.8f}",
            "cache_cost": f"{costs[2]:.8f}", "total_cost": f"{costs[3]:.8f}",
            "success_count": int(aggregate[9]), "failed_count": int(aggregate[10]), "unknown_usage_count": int(aggregate[11]),
            "daily_trend": [{"billing_date": row[0], "total_tokens": int(row[1]), "total_cost": f"{Decimal(int(row[2] or 0)) / Decimal(100_000_000):.8f}"} for row in trend_rows],
        }

    async def get_billing_accounts(
        self,
        range_name: str = "today",
        page: int = 1,
        page_size: int = 50,
        api_key_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._get_billing_ranking(
            range_name, page, page_size, "credential_name", api_key_id=api_key_id
        )

    async def get_billing_models(
        self,
        range_name: str = "today",
        page: int = 1,
        page_size: int = 50,
        api_key_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._get_billing_ranking(
            range_name, page, page_size, "model", api_key_id=api_key_id
        )

    async def _get_billing_ranking(
        self,
        range_name: str,
        page: int,
        page_size: int,
        dimension: str,
        *,
        api_key_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if dimension not in {"credential_name", "model"}:
            raise ValueError("unsupported billing dimension")
        dates = self._billing_range_dates(range_name)
        start_date, end_date = dates[-1], dates[0]
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 100)
        offset = (page - 1) * page_size
        key_filter = " AND api_key_id = ?" if api_key_id else ""
        base_params: List[Any] = [start_date, end_date]
        if api_key_id:
            base_params.append(api_key_id)
        async with aiosqlite.connect(self._db_path) as db:
            await self._register_money_functions(db)
            async with db.execute(
                f"SELECT COUNT(*) FROM (SELECT {dimension}, currency FROM billing_daily WHERE billing_date BETWEEN ? AND ?{key_filter} GROUP BY {dimension}, currency)",
                base_params,
            ) as cursor:
                total = int((await cursor.fetchone())[0])
            async with db.execute(
                f"""SELECT {dimension}, currency, COALESCE(SUM(total_tokens), 0),
                           COALESCE(SUM(money_e8(total_cost)), 0),
                           COALESCE(SUM(success_count), 0), COALESCE(SUM(failed_count), 0),
                           COALESCE(SUM(unknown_usage_count), 0)
                    FROM billing_daily WHERE billing_date BETWEEN ? AND ?{key_filter}
                    GROUP BY {dimension}, currency
                    ORDER BY SUM(total_tokens) DESC, {dimension} ASC LIMIT ? OFFSET ?""",
                [*base_params, page_size, offset],
            ) as cursor:
                rows = await cursor.fetchall()
        items = [{dimension: row[0], "currency": row[1], "total_tokens": int(row[2]),
                  "total_cost": f"{Decimal(int(row[3] or 0)) / Decimal(100_000_000):.8f}", "success_count": int(row[4]),
                  "failed_count": int(row[5]), "unknown_usage_count": int(row[6])} for row in rows]
        return {"items": items, "page": page, "page_size": page_size, "total": total, "has_more": offset + page_size < total}

    async def get_billing_keys(
        self, range_name: str = "today", page: int = 1, page_size: int = 50
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        dates = self._billing_range_dates(range_name)
        start_date, end_date = dates[-1], dates[0]
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 100)
        offset = (page - 1) * page_size
        async with aiosqlite.connect(self._db_path) as db:
            await self._register_money_functions(db)
            async with db.execute(
                """SELECT COUNT(*) FROM (
                       SELECT api_key_id, currency FROM billing_daily
                       WHERE billing_date BETWEEN ? AND ?
                       GROUP BY api_key_id, currency
                   )""",
                (start_date, end_date),
            ) as cursor:
                total = int((await cursor.fetchone())[0])
            async with db.execute(
                """SELECT d.api_key_id, COALESCE(k.name, d.api_key_id),
                          COALESCE(k.key_prefix, ''), d.currency,
                          COALESCE(SUM(d.total_tokens), 0),
                          COALESCE(SUM(money_e8(d.total_cost)), 0),
                          COALESCE(SUM(d.success_count), 0),
                          COALESCE(SUM(d.failed_count), 0),
                          COALESCE(SUM(d.unknown_usage_count), 0)
                   FROM billing_daily d
                   LEFT JOIN api_keys k ON k.id = d.api_key_id
                   WHERE d.billing_date BETWEEN ? AND ?
                   GROUP BY d.api_key_id, k.name, k.key_prefix, d.currency
                   ORDER BY SUM(d.total_tokens) DESC, COALESCE(k.name, d.api_key_id) ASC
                   LIMIT ? OFFSET ?""",
                (start_date, end_date, page_size, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        items = [
            {
                "api_key_id": row[0],
                "api_key_name": row[1],
                "key_prefix": row[2],
                "currency": row[3],
                "total_tokens": int(row[4]),
                "total_cost": f"{Decimal(int(row[5] or 0)) / Decimal(100_000_000):.8f}",
                "success_count": int(row[6]),
                "failed_count": int(row[7]),
                "unknown_usage_count": int(row[8]),
            }
            for row in rows
        ]
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": offset + page_size < total,
        }

    async def list_billing_daily(
        self, days: int = 45, page: int = 1, page_size: int = 500
    ) -> List[Dict[str, Any]]:
        """Return one bounded page for Redis mirror reconstruction."""
        self._ensure_initialized()
        days = min(max(int(days), 1), 45)
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 500)
        offset = (page - 1) * page_size
        cutoff = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=days - 1)).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT billing_date, api_key_id, credential_name, model, currency,
                          input_tokens, output_tokens, cache_tokens, thought_tokens, total_tokens,
                          input_cost, output_cost, cache_cost, total_cost,
                          success_count, failed_count, unknown_usage_count
                   FROM billing_daily WHERE billing_date >= ?
                   ORDER BY billing_date, api_key_id, credential_name, model, currency
                   LIMIT ? OFFSET ?""",
                (cutoff, page_size, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        keys = ("billing_date", "api_key_id", "credential_name", "model", "currency", "input_tokens", "output_tokens", "cache_tokens", "thought_tokens", "total_tokens", "input_cost", "output_cost", "cache_cost", "total_cost", "success_count", "failed_count", "unknown_usage_count")
        return [dict(zip(keys, row)) for row in rows]

    async def list_credentials(self, mode: str = "geminicli") -> List[str]:
        """列出所有凭证文件名（包括禁用的）"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                async with db.execute(f"""
                    SELECT filename FROM {table_name} ORDER BY rotation_order
                """) as cursor:
                    rows = await cursor.fetchall()
                    return [row[0] for row in rows]

        except Exception as e:
            log.error(f"Error listing credentials: {e}")
            return []

    async def delete_credential(self, filename: str, mode: str = "geminicli") -> bool:
        """删除凭证"""
        self._ensure_initialized()

        # 统一使用 basename 处理文件名
        filename = os.path.basename(filename)

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                # 精确匹配删除
                result = await db.execute(f"""
                    DELETE FROM {table_name} WHERE filename = ?
                """, (filename,))
                deleted_count = result.rowcount

                await db.commit()

                if deleted_count > 0:
                    log.debug(f"Deleted {deleted_count} credential(s): {filename} (mode={mode})")
                    return True
                else:
                    log.warning(f"No credential found to delete: {filename} (mode={mode})")
                    return False

        except Exception as e:
            log.error(f"Error deleting credential {filename}: {e}")
            return False

    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any], mode: str = "geminicli") -> bool:
        """更新凭证状态"""
        self._ensure_initialized()

        # 统一使用 basename 处理文件名
        filename = os.path.basename(filename)

        try:
            table_name = self._get_table_name(mode)
            log.debug(f"[DB] update_credential_state 开始: filename={filename}, state_updates={state_updates}, mode={mode}, table={table_name}")

            # 构建动态 SQL
            set_clauses = []
            values = []

            for key, value in state_updates.items():
                if key in self.STATE_FIELDS:
                    if key == "enable_credit" and mode != "antigravity":
                        continue
                    if key in ("error_codes", "error_messages", "model_cooldowns"):
                        # JSON 字段需要序列化
                        set_clauses.append(f"{key} = ?")
                        values.append(json.dumps(value))
                    else:
                        set_clauses.append(f"{key} = ?")
                        values.append(value)

            if not set_clauses:
                log.info(f"[DB] 没有需要更新的状态字段")
                return True

            set_clauses.append("updated_at = unixepoch()")
            values.append(filename)

            log.debug(f"[DB] SQL参数: set_clauses={set_clauses}, values={values}")

            async with aiosqlite.connect(self._db_path) as db:
                # 精确匹配更新
                sql_exact = f"""
                    UPDATE {table_name}
                    SET {', '.join(set_clauses)}
                    WHERE filename = ?
                """
                log.debug(f"[DB] 执行精确匹配SQL: {sql_exact}")
                log.debug(f"[DB] SQL参数值: {values}")

                result = await db.execute(sql_exact, values)
                updated_count = result.rowcount
                log.debug(f"[DB] 精确匹配 rowcount={updated_count}")

                # 提交前检查
                log.debug(f"[DB] 准备commit，总更新行数={updated_count}")
                await db.commit()
                log.debug(f"[DB] commit完成")

                success = updated_count > 0
                log.debug(f"[DB] update_credential_state 结束: success={success}, updated_count={updated_count}")
                return success

        except Exception as e:
            log.error(f"[DB] Error updating credential state {filename}: {e}")
            return False

    async def get_credential_state(self, filename: str, mode: str = "geminicli") -> Dict[str, Any]:
        """获取凭证状态（不包含error_messages）"""
        self._ensure_initialized()

        # 统一使用 basename 处理文件名
        filename = os.path.basename(filename)

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                # 精确匹配
                if mode == "geminicli":
                    async with db.execute(f"""
                        SELECT disabled, error_codes, last_success, user_email, model_cooldowns, preview, tier
                        FROM {table_name} WHERE filename = ?
                    """, (filename,)) as cursor:
                        row = await cursor.fetchone()

                        if row:
                            error_codes_json = row[1] or "[]"
                            model_cooldowns_json = row[4] or "{}"
                            active_cooldowns = _active_model_cooldowns(
                                json.loads(model_cooldowns_json), time.time()
                            )
                            return {
                                "disabled": bool(row[0]),
                                "error_codes": _visible_error_codes(
                                    json.loads(error_codes_json),
                                    mode=mode,
                                    active_cooldowns=active_cooldowns,
                                ),
                                "last_success": row[2] or time.time(),
                                "user_email": row[3],
                                "model_cooldowns": active_cooldowns,
                                "preview": bool(row[5]) if row[5] is not None else True,
                                "tier": row[6] if row[6] is not None else "pro",
                            }

                    # 返回默认状态
                    return {
                        "disabled": False,
                        "error_codes": [],
                        "last_success": time.time(),
                        "user_email": None,
                        "model_cooldowns": {},
                        "preview": True,
                        "tier": "pro",
                    }
                else:
                    # antigravity 模式
                    async with db.execute(
                        f"""
                        SELECT disabled, error_codes, last_success, user_email, model_cooldowns, tier, enable_credit
                        FROM {table_name} WHERE filename = ?
                    """,
                        (filename,),
                    ) as cursor:
                        row = await cursor.fetchone()

                        if row:
                            error_codes_json = row[1] or "[]"
                            model_cooldowns_json = row[4] or "{}"
                            active_cooldowns = _active_model_cooldowns(
                                json.loads(model_cooldowns_json), time.time()
                            )
                            return {
                                "disabled": bool(row[0]),
                                "error_codes": _visible_error_codes(
                                    json.loads(error_codes_json),
                                    mode=mode,
                                    active_cooldowns=active_cooldowns,
                                ),
                                "last_success": row[2] or time.time(),
                                "user_email": row[3],
                                "model_cooldowns": active_cooldowns,
                                "tier": row[5] if row[5] is not None else "pro",
                                "enable_credit": bool(row[6]) if row[6] is not None else False,
                            }

                    # 返回默认状态
                    return {
                        "disabled": False,
                        "error_codes": [],
                        "last_success": time.time(),
                        "user_email": None,
                        "model_cooldowns": {},
                        "tier": "pro",
                        "enable_credit": False,
                    }

        except Exception as e:
            log.error(f"Error getting credential state {filename}: {e}")
            return {}

    async def get_all_credential_states(self, mode: str = "geminicli") -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态（不包含error_messages）"""
        self._ensure_initialized()

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                if mode == "geminicli":
                    async with db.execute(f"""
                        SELECT filename, disabled, error_codes, last_success,
                               user_email, model_cooldowns, preview, tier
                        FROM {table_name}
                    """) as cursor:
                        rows = await cursor.fetchall()

                        states = {}
                        current_time = time.time()

                        for row in rows:
                            filename = row[0]
                            error_codes_json = row[2] or "[]"
                            model_cooldowns_json = row[5] or "{}"
                            model_cooldowns = _active_model_cooldowns(
                                json.loads(model_cooldowns_json), current_time
                            )

                            states[filename] = {
                                "disabled": bool(row[1]),
                                "error_codes": _visible_error_codes(
                                    json.loads(error_codes_json),
                                    mode=mode,
                                    active_cooldowns=model_cooldowns,
                                ),
                                "last_success": row[3] or time.time(),
                                "user_email": row[4],
                                "model_cooldowns": model_cooldowns,
                                "preview": bool(row[6]) if row[6] is not None else True,
                                "tier": row[7] if row[7] is not None else "pro",
                            }

                        return states
                else:
                    # antigravity 模式
                    async with db.execute(f"""
                        SELECT filename, disabled, error_codes, last_success,
                               user_email, model_cooldowns, tier, enable_credit,
                               call_count, rotation_order
                        FROM {table_name}
                    """) as cursor:
                        rows = await cursor.fetchall()

                        states = {}
                        current_time = time.time()

                        for row in rows:
                            filename = row[0]
                            error_codes_json = row[2] or "[]"
                            model_cooldowns_json = row[5] or "{}"
                            model_cooldowns = _active_model_cooldowns(
                                json.loads(model_cooldowns_json), current_time
                            )

                            states[filename] = {
                                "disabled": bool(row[1]),
                                "error_codes": _visible_error_codes(
                                    json.loads(error_codes_json),
                                    mode=mode,
                                    active_cooldowns=model_cooldowns,
                                ),
                                "last_success": row[3] or time.time(),
                                "user_email": row[4],
                                "model_cooldowns": model_cooldowns,
                                "tier": row[6] if row[6] is not None else "pro",
                                "enable_credit": bool(row[7]) if row[7] is not None else False,
                                "call_count": max(int(row[8] or 0), 0),
                                "rotation_order": max(int(row[9] or 0), 0),
                            }

                        return states

        except Exception as e:
            log.error(f"Error getting all credential states: {e}")
            return {}

    async def get_credentials_summary(
        self,
        offset: int = 0,
        limit: Optional[int] = None,
        status_filter: str = "all",
        mode: str = "geminicli",
        error_code_filter: Optional[str] = None,
        cooldown_filter: Optional[str] = None,
        preview_filter: Optional[str] = None,
        tier_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取凭证的摘要信息（不包含完整凭证数据）- 支持分页和状态筛选

        Args:
            offset: 跳过的记录数（默认0）
            limit: 返回的最大记录数（None表示返回所有）
            status_filter: 状态筛选（all=全部, enabled=仅启用, disabled=仅禁用）
            mode: 凭证模式 ("geminicli" 或 "antigravity")
            error_code_filter: 错误码筛选（格式如"400"或"403"，筛选包含该错误码的凭证）
            cooldown_filter: 冷却状态筛选（"in_cooldown"=冷却中, "no_cooldown"=未冷却）
            preview_filter: Preview筛选（"preview"=支持preview, "no_preview"=不支持preview，仅geminicli模式有效）
            tier_filter: tier筛选（"free", "pro", "ultra"）

        Returns:
            包含 items（凭证列表）、total（总数）、offset、limit 的字典
        """
        self._ensure_initialized()

        try:
            # 根据 mode 选择表名
            table_name = self._get_table_name(mode)

            async with aiosqlite.connect(self._db_path) as db:
                # 先计算全局统计数据（不受筛选条件影响）
                global_stats = {"total": 0, "normal": 0, "disabled": 0}
                async with db.execute(f"""
                    SELECT disabled, COUNT(*) FROM {table_name} GROUP BY disabled
                """) as stats_cursor:
                    stats_rows = await stats_cursor.fetchall()
                    for disabled, count in stats_rows:
                        global_stats["total"] += count
                        if disabled:
                            global_stats["disabled"] = count
                        else:
                            global_stats["normal"] = count

                # 构建WHERE子句
                where_clauses = []
                count_params = []

                if status_filter == "enabled":
                    where_clauses.append("disabled = 0")
                elif status_filter == "disabled":
                    where_clauses.append("disabled = 1")

                filter_value = None
                filter_int = None
                filter_none = False
                if error_code_filter and str(error_code_filter).strip().lower() != "all":
                    if str(error_code_filter).strip().lower() == "none":
                        filter_none = True
                    else:
                        filter_value = str(error_code_filter).strip()
                        try:
                            filter_int = int(filter_value)
                        except ValueError:
                            filter_int = None

                # 构建WHERE子句
                where_clause = ""
                if where_clauses:
                    where_clause = "WHERE " + " AND ".join(where_clauses)

                # 先获取所有数据（用于冷却筛选，因为需要在Python中判断）
                if mode == "geminicli":
                    all_query = f"""
                        SELECT filename, disabled, error_codes, last_success,
                               user_email, rotation_order, model_cooldowns, preview, tier
                        FROM {table_name}
                        {where_clause}
                        ORDER BY rotation_order
                    """
                else:
                    all_query = f"""
                        SELECT a.filename, a.disabled, a.error_codes, a.last_success,
                               a.user_email, a.rotation_order, a.model_cooldowns,
                               a.tier, a.enable_credit, a.proxy_mode, a.proxy_group_id,
                               a.bound_proxy_node_id, n.name,
                               h.egress_ip, h.egress_country, h.binding_status,
                               h.eligibility_status, h.eligibility_checked_at,
                               h.last_403_category
                        FROM {table_name} a
                        LEFT JOIN proxy_group_nodes n ON n.id = a.bound_proxy_node_id
                        LEFT JOIN antigravity_account_health h
                               ON h.credential_name = a.filename
                        {where_clause}
                        ORDER BY a.rotation_order
                    """

                async with db.execute(all_query, count_params) as cursor:
                    all_rows = await cursor.fetchall()

                    current_time = time.time()
                    all_summaries = []

                    for row in all_rows:
                        filename = row[0]
                        error_codes_json = row[2] or "[]"
                        model_cooldowns_json = row[6] or "{}"
                        active_cooldowns = _active_model_cooldowns(
                            json.loads(model_cooldowns_json), current_time
                        )
                        error_codes = _visible_error_codes(
                            json.loads(error_codes_json),
                            mode=mode,
                            active_cooldowns=active_cooldowns,
                        )

                        # 筛选无错误的凭证
                        if filter_none:
                            if error_codes:
                                continue

                        if filter_value:
                            match = False
                            for code in error_codes:
                                if code == filter_value or code == filter_int:
                                    match = True
                                    break
                                if isinstance(code, str) and filter_int is not None:
                                    try:
                                        if int(code) == filter_int:
                                            match = True
                                            break
                                    except ValueError:
                                        pass
                            if not match:
                                continue

                        summary = {
                            "filename": filename,
                            "disabled": bool(row[1]),
                            "error_codes": error_codes,
                            "last_success": row[3] or current_time,
                            "user_email": row[4],
                            "rotation_order": row[5],
                            "model_cooldowns": active_cooldowns,
                            "tier": row[8] if mode == "geminicli" and row[8] is not None else (
                                row[7] if mode != "geminicli" and row[7] is not None else "pro"
                            ),
                        }

                        if mode != "geminicli":
                            summary["enable_credit"] = bool(row[8]) if row[8] is not None else False
                            summary["proxy_mode"] = row[9] or "inherit"
                            summary["proxy_group_id"] = row[10]
                            summary["bound_proxy_node_id"] = row[11]
                            summary["bound_proxy_node_name"] = row[12]
                            summary["egress_ip"] = row[13]
                            summary["egress_country"] = row[14]
                            summary["binding_status"] = row[15] or "unchecked"
                            summary["eligibility_status"] = row[16] or "unchecked"
                            summary["eligibility_checked_at"] = row[17]
                            summary["last_403_category"] = row[18]

                        if mode == "geminicli":
                            summary["preview"] = bool(row[7]) if row[7] is not None else True

                            if preview_filter:
                                preview_value = summary.get("preview", True)
                                if preview_filter == "preview" and not preview_value:
                                    continue
                                elif preview_filter == "no_preview" and preview_value:
                                    continue

                        # 应用tier筛选
                        if tier_filter and tier_filter in ("free", "pro", "ultra"):
                            if summary["tier"] != tier_filter:
                                continue

                        # 应用冷却筛选
                        if cooldown_filter == "in_cooldown":
                            # 只保留有冷却的凭证
                            if active_cooldowns:
                                all_summaries.append(summary)
                        elif cooldown_filter == "no_cooldown":
                            # 只保留没有冷却的凭证
                            if not active_cooldowns:
                                all_summaries.append(summary)
                        else:
                            # 不筛选冷却状态
                            all_summaries.append(summary)

                    # 应用分页
                    total_count = len(all_summaries)
                    if limit is not None:
                        summaries = all_summaries[offset:offset + limit]
                    else:
                        summaries = all_summaries[offset:]

                    return {
                        "items": summaries,
                        "total": total_count,
                        "offset": offset,
                        "limit": limit,
                        "stats": global_stats,
                    }

        except Exception as e:
            log.error(f"Error getting credentials summary: {e}")
            return {
                "items": [],
                "total": 0,
                "offset": offset,
                "limit": limit,
                "stats": {"total": 0, "normal": 0, "disabled": 0},
            }

    async def get_duplicate_credentials_by_email(self, mode: str = "geminicli") -> Dict[str, Any]:
        """
        获取按邮箱分组的重复凭证信息（只查询邮箱和文件名，不加载完整凭证数据）
        用于去重操作

        Args:
            mode: 凭证模式 ("geminicli" 或 "antigravity")

        Returns:
            包含 email_groups（邮箱分组）、duplicate_count（重复数量）、no_email_count（无邮箱数量）的字典
        """
        self._ensure_initialized()

        try:
            # 根据 mode 选择表名
            table_name = self._get_table_name(mode)

            async with aiosqlite.connect(self._db_path) as db:
                # 查询所有凭证的文件名和邮箱（不加载完整凭证数据）
                query = f"""
                    SELECT filename, user_email
                    FROM {table_name}
                    ORDER BY filename
                """

                async with db.execute(query) as cursor:
                    rows = await cursor.fetchall()

                    # 按邮箱分组
                    email_to_files = {}
                    no_email_files = []

                    for filename, user_email in rows:
                        if user_email:
                            if user_email not in email_to_files:
                                email_to_files[user_email] = []
                            email_to_files[user_email].append(filename)
                        else:
                            no_email_files.append(filename)

                    # 找出重复的邮箱组
                    duplicate_groups = []
                    total_duplicate_count = 0

                    for email, files in email_to_files.items():
                        if len(files) > 1:
                            # 保留第一个文件，其他为重复
                            duplicate_groups.append({
                                "email": email,
                                "kept_file": files[0],
                                "duplicate_files": files[1:],
                                "duplicate_count": len(files) - 1,
                            })
                            total_duplicate_count += len(files) - 1

                    return {
                        "email_groups": email_to_files,
                        "duplicate_groups": duplicate_groups,
                        "duplicate_count": total_duplicate_count,
                        "no_email_files": no_email_files,
                        "no_email_count": len(no_email_files),
                        "unique_email_count": len(email_to_files),
                        "total_count": len(rows),
                    }

        except Exception as e:
            log.error(f"Error getting duplicate credentials by email: {e}")
            return {
                "email_groups": {},
                "duplicate_groups": [],
                "duplicate_count": 0,
                "no_email_files": [],
                "no_email_count": 0,
                "unique_email_count": 0,
                "total_count": 0,
            }

    # ============ 配置管理（内存缓存）============

    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置（写入数据库 + 更新内存缓存）"""
        self._ensure_initialized()

        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("""
                    INSERT INTO config (key, value, updated_at)
                    VALUES (?, ?, unixepoch())
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                """, (key, json.dumps(value)))
                await db.commit()

            # 更新内存缓存
            self._config_cache[key] = value
            return True

        except Exception as e:
            log.error(f"Error setting config {key}: {e}")
            return False

    async def reload_config_cache(self):
        """重新加载配置缓存（在批量修改配置后调用）"""
        self._ensure_initialized()
        self._config_loaded = False
        await self._load_config_cache()
        log.info("Config cache reloaded from database")

    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置（从内存缓存）"""
        self._ensure_initialized()
        return self._config_cache.get(key, default)

    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置（从内存缓存）"""
        self._ensure_initialized()
        return self._config_cache.copy()

    async def delete_config(self, key: str) -> bool:
        """删除配置"""
        self._ensure_initialized()

        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("DELETE FROM config WHERE key = ?", (key,))
                await db.commit()

            # 从内存缓存移除
            self._config_cache.pop(key, None)
            return True

        except Exception as e:
            log.error(f"Error deleting config {key}: {e}")
            return False

    async def get_credential_errors(self, filename: str, mode: str = "geminicli") -> Dict[str, Any]:
        """
        专门获取凭证的错误信息（包含 error_codes 和 error_messages）

        Args:
            filename: 凭证文件名
            mode: 凭证模式 ("geminicli" 或 "antigravity")

        Returns:
            包含 error_codes 和 error_messages 的字典
        """
        self._ensure_initialized()

        # 统一使用 basename 处理文件名
        filename = os.path.basename(filename)

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                # 精确匹配
                async with db.execute(f"""
                    SELECT error_codes, error_messages FROM {table_name} WHERE filename = ?
                """, (filename,)) as cursor:
                    row = await cursor.fetchone()

                    if row:
                        error_codes_json = row[0] or '[]'
                        error_messages_json = row[1] or '[]'
                        return {
                            "filename": filename,
                            "error_codes": json.loads(error_codes_json),
                            "error_messages": json.loads(error_messages_json),
                        }

                # 凭证不存在，返回空错误信息
                return {
                    "filename": filename,
                    "error_codes": [],
                    "error_messages": [],
                }

        except Exception as e:
            log.error(f"Error getting credential errors {filename}: {e}")
            return {
                "filename": filename,
                "error_codes": [],
                "error_messages": [],
                "error": str(e)
            }

    # ============ 模型级冷却管理 ============

    async def set_model_cooldown(
        self,
        filename: str,
        model_name: str,
        cooldown_until: Optional[float],
        mode: str = "geminicli"
    ) -> bool:
        """
        设置特定模型的冷却时间

        Args:
            filename: 凭证文件名
            model_name: 模型名（完整模型名，如 "gemini-2.0-flash-exp"）
            cooldown_until: 冷却截止时间戳（None 表示清除冷却）
            mode: 凭证模式 ("geminicli" 或 "antigravity")

        Returns:
            是否成功
        """
        self._ensure_initialized()

        # 统一使用 basename 处理文件名
        filename = os.path.basename(filename)

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                # 获取当前的 model_cooldowns
                async with db.execute(f"""
                    SELECT model_cooldowns FROM {table_name} WHERE filename = ?
                """, (filename,)) as cursor:
                    row = await cursor.fetchone()

                    if not row:
                        log.warning(f"Credential {filename} not found")
                        return False

                    model_cooldowns = json.loads(row[0] or '{}')

                    # 更新或删除指定模型的冷却时间
                    if cooldown_until is None:
                        model_cooldowns.pop(model_name, None)
                    else:
                        model_cooldowns[model_name] = cooldown_until

                    # 写回数据库
                    await db.execute(f"""
                        UPDATE {table_name}
                        SET model_cooldowns = ?,
                            updated_at = unixepoch()
                        WHERE filename = ?
                    """, (json.dumps(model_cooldowns), filename))
                    await db.commit()

                    log.debug(f"Set model cooldown: {filename}, model_name={model_name}, cooldown_until={cooldown_until}")
                    return True

        except Exception as e:
            log.error(f"Error setting model cooldown for {filename}: {e}")
            return False

    async def clear_all_model_cooldowns(
        self,
        filename: str,
        mode: str = "geminicli"
    ) -> bool:
        """清除某个凭证的所有模型冷却时间"""
        self._ensure_initialized()

        filename = os.path.basename(filename)

        try:
            table_name = self._get_table_name(mode)
            async with aiosqlite.connect(self._db_path) as db:
                result = await db.execute(f"""
                    UPDATE {table_name}
                    SET model_cooldowns = '{{}}',
                        updated_at = unixepoch()
                    WHERE filename = ?
                """, (filename,))
                updated_count = result.rowcount
                await db.commit()

            if updated_count == 0:
                log.warning(f"Credential {filename} not found")
                return False

            log.debug(f"Cleared all model cooldowns: {filename} (mode={mode})")
            return True

        except Exception as e:
            log.error(f"Error clearing all model cooldowns for {filename}: {e}")
            return False

    async def record_success(
        self,
        filename: str,
        model_name: Optional[str] = None,
        mode: str = "geminicli"
    ) -> None:
        """
        成功调用后的条件写入：
        - 只有当前 error_codes 非空时才清除错误并写 last_success
        - 只有当前存在该模型的冷却键时才清除
        通过 SQL WHERE 条件匹配实现
        """
        self._ensure_initialized()
        filename = os.path.basename(filename)

        try:
            table_name = self._get_table_name(mode)
            # 成功响应可能在短时间内大量并发到达。这里只允许一个 SQLite
            # 成功状态事务持有连接，防止 aiosqlite 连接/线程堆积耗尽 nofile。
            async with self._success_write_lock:
                async with aiosqlite.connect(self._db_path) as db:
                    await db.execute("PRAGMA busy_timeout = 5000")

                    # 条件写入：只有 error_codes 非空时才触发
                    await db.execute(f"""
                        UPDATE {table_name}
                        SET last_success = unixepoch(),
                            error_codes   = '[]',
                            error_messages = '{{}}',
                            updated_at    = unixepoch()
                        WHERE filename = ?
                          AND (error_codes IS NOT NULL AND error_codes != '[]' AND error_codes != '')
                    """, (filename,))

                    # 条件删除模型冷却：只有模型键存在时才写入
                    if model_name:
                        async with db.execute(f"""
                            SELECT model_cooldowns FROM {table_name} WHERE filename = ?
                        """, (filename,)) as cursor:
                            row = await cursor.fetchone()
                            if row:
                                cooldowns = json.loads(row[0] or '{}')
                                if model_name in cooldowns:
                                    cooldowns.pop(model_name)
                                    await db.execute(f"""
                                        UPDATE {table_name}
                                        SET model_cooldowns = ?, updated_at = unixepoch()
                                        WHERE filename = ?
                                    """, (json.dumps(cooldowns), filename))

                    if mode == "antigravity":
                        # 一次真实 API 成功比陈旧的 403/地区缓存更可信。清除
                        # 账号级红色状态；模型级冷却仍只清除本次成功的模型。
                        await db.execute("""
                            UPDATE antigravity_account_health
                            SET binding_status = CASE
                                    WHEN binding_status IN ('proxy_drift', 'proxy_failed')
                                    THEN 'healthy' ELSE binding_status END,
                                eligibility_status = 'eligible',
                                eligibility_reason = NULL,
                                eligibility_checked_at = unixepoch(),
                                last_403_category = NULL,
                                last_403_reason = NULL,
                                last_403_at = NULL,
                                updated_at = unixepoch()
                            WHERE credential_name = ?
                              AND (
                                  last_403_category IS NOT NULL
                                  OR eligibility_status IN (
                                      'geo_blocked', 'account_blocked',
                                      'validation_required', 'error'
                                  )
                                  OR binding_status IN ('proxy_drift', 'proxy_failed')
                              )
                        """, (filename,))

                    await db.commit()

        except Exception as e:
            log.error(f"Error recording success for {filename}: {e}")

    async def increment_model_stats(
        self,
        filename: str,
        model_name: str,
        success: bool,
        status_code: Optional[int] = None,
        mode: str = "geminicli",
        upstream_seconds: Optional[float] = None,
        gateway_seconds: Optional[float] = None,
    ) -> None:
        """累计「凭证 × 模型」的调用成功/失败次数（供面板成功率统计）。

        同时写两行：全量累计（credential_model_stats）和按日累计
        （credential_model_stats_daily，stat_date 按北京时间划分，与面板显示一致）。
        upstream_seconds / gateway_seconds 非 None 时累加耗时并 +1 计数（供求均值）。
        """
        self._ensure_initialized()
        if not model_name:
            return
        filename = os.path.basename(filename)
        stat_date = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        upstream_inc = float(upstream_seconds) if upstream_seconds is not None else 0.0
        upstream_cnt = 1 if upstream_seconds is not None else 0
        gateway_inc = float(gateway_seconds) if gateway_seconds is not None else 0.0
        gateway_cnt = 1 if gateway_seconds is not None else 0

        try:
            # 与成功状态写入同理：串行化轻量统计写入，避免高并发下连接堆积
            async with self._stats_write_lock:
                async with aiosqlite.connect(self._db_path) as db:
                    await db.execute("PRAGMA busy_timeout = 5000")
                    await db.execute("""
                        INSERT INTO credential_model_stats
                            (mode, credential_name, model_name,
                             success_count, failed_count, last_status,
                             upstream_total_seconds, upstream_timed_count,
                             gateway_total_seconds, gateway_timed_count,
                             updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())
                        ON CONFLICT (mode, credential_name, model_name) DO UPDATE SET
                            success_count = success_count + excluded.success_count,
                            failed_count  = failed_count + excluded.failed_count,
                            last_status   = excluded.last_status,
                            upstream_total_seconds = upstream_total_seconds + excluded.upstream_total_seconds,
                            upstream_timed_count   = upstream_timed_count + excluded.upstream_timed_count,
                            gateway_total_seconds  = gateway_total_seconds + excluded.gateway_total_seconds,
                            gateway_timed_count    = gateway_timed_count + excluded.gateway_timed_count,
                            updated_at    = excluded.updated_at
                    """, (
                        mode, filename, model_name,
                        1 if success else 0,
                        0 if success else 1,
                        status_code,
                        upstream_inc, upstream_cnt,
                        gateway_inc, gateway_cnt,
                    ))
                    await db.execute("""
                        INSERT INTO credential_model_stats_daily
                            (mode, stat_date, credential_name, model_name,
                             success_count, failed_count, last_status,
                             upstream_total_seconds, upstream_timed_count,
                             gateway_total_seconds, gateway_timed_count,
                             updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())
                        ON CONFLICT (mode, stat_date, credential_name, model_name)
                        DO UPDATE SET
                            success_count = success_count + excluded.success_count,
                            failed_count  = failed_count + excluded.failed_count,
                            last_status   = excluded.last_status,
                            upstream_total_seconds = upstream_total_seconds + excluded.upstream_total_seconds,
                            upstream_timed_count   = upstream_timed_count + excluded.upstream_timed_count,
                            gateway_total_seconds  = gateway_total_seconds + excluded.gateway_total_seconds,
                            gateway_timed_count    = gateway_timed_count + excluded.gateway_timed_count,
                            updated_at    = excluded.updated_at
                    """, (
                        mode, stat_date, filename, model_name,
                        1 if success else 0,
                        0 if success else 1,
                        status_code,
                        upstream_inc, upstream_cnt,
                        gateway_inc, gateway_cnt,
                    ))
                    await db.commit()
        except Exception as e:
            log.error(f"Error incrementing model stats for {filename}/{model_name}: {e}")

    async def get_model_stats(
        self, mode: str = "geminicli", date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """读取指定模式下「凭证 × 模型」调用统计。

        date 为 None 时返回全量累计；否则返回该日（YYYY-MM-DD，北京时间）的按日统计。
        每行附带账号邮箱（user_email）和套餐级别（tier），LEFT JOIN 凭证表，缺失时为 None。
        """
        self._ensure_initialized()
        table_name = self._get_table_name(mode)

        try:
            async with aiosqlite.connect(self._db_path) as db:
                if date:
                    query = f"""
                        SELECT s.credential_name, s.model_name,
                               s.success_count, s.failed_count, s.last_status, s.updated_at,
                               c.user_email, c.tier,
                               s.upstream_total_seconds, s.upstream_timed_count,
                               s.gateway_total_seconds, s.gateway_timed_count
                        FROM credential_model_stats_daily s
                        LEFT JOIN {table_name} c ON c.filename = s.credential_name
                        WHERE s.mode = ? AND s.stat_date = ?
                        ORDER BY s.credential_name, s.model_name
                    """
                    params: tuple = (mode, date)
                else:
                    query = f"""
                        SELECT s.credential_name, s.model_name,
                               s.success_count, s.failed_count, s.last_status, s.updated_at,
                               c.user_email, c.tier,
                               s.upstream_total_seconds, s.upstream_timed_count,
                               s.gateway_total_seconds, s.gateway_timed_count
                        FROM credential_model_stats s
                        LEFT JOIN {table_name} c ON c.filename = s.credential_name
                        WHERE s.mode = ?
                        ORDER BY s.credential_name, s.model_name
                    """
                    params = (mode,)
                async with db.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [
                        {
                            "credential_name": row[0],
                            "model_name": row[1],
                            "success_count": row[2],
                            "failed_count": row[3],
                            "last_status": row[4],
                            "updated_at": row[5],
                            "user_email": row[6],
                            "tier": row[7],
                            "upstream_total_seconds": row[8],
                            "upstream_timed_count": row[9],
                            "gateway_total_seconds": row[10],
                            "gateway_timed_count": row[11],
                        }
                        for row in rows
                    ]
        except Exception as e:
            log.error(f"Error reading model stats (mode={mode}, date={date}): {e}")
            return []
