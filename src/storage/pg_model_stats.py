"""PostgreSQL increments on top of the existing SQLite model-statistics history."""
from contextlib import AsyncExitStack

import asyncpg

from src.request_timing import measure_pg

COUNTERS = (
    "success_count", "failed_count", "upstream_total_seconds", "upstream_timed_count",
    "gateway_total_seconds", "gateway_timed_count",
)


class PostgresModelStats:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def open(cls, dsn):
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, timeout=4, command_timeout=4)
        try:
            async with pool.acquire(timeout=4) as conn, conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(487765920)")
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS model_stat_increments (
                        mode text NOT NULL, period text NOT NULL, credential_name text NOT NULL,
                        model_name text NOT NULL, success_count bigint NOT NULL,
                        failed_count bigint NOT NULL, upstream_total_seconds double precision NOT NULL,
                        upstream_timed_count bigint NOT NULL, gateway_total_seconds double precision NOT NULL,
                        gateway_timed_count bigint NOT NULL, last_status integer, updated_at double precision NOT NULL,
                        PRIMARY KEY(mode, period, credential_name, model_name)
                    )
                """)
        except BaseException:
            await pool.close()
            raise
        return cls(pool)

    async def increment(self, mode, day, credential, model, success, status, upstream, gateway):
        # Both counters commit in one statement. All writers take the total row first.
        async with AsyncExitStack() as stack:
            with measure_pg("pool"):
                conn = await stack.enter_async_context(self.pool.acquire(timeout=4))
            with measure_pg("sql"):
                await conn.execute("""
                    INSERT INTO model_stat_increments AS s
                        (mode, period, credential_name, model_name, success_count, failed_count,
                         upstream_total_seconds, upstream_timed_count, gateway_total_seconds,
                         gateway_timed_count, last_status, updated_at)
                    SELECT $1,p,$3,$4,$5,$6,$7,$8,$9,$10,$11,extract(epoch from statement_timestamp())
                    FROM (VALUES ('*'), ($2::text)) periods(p) ORDER BY p
                    ON CONFLICT (mode,period,credential_name,model_name) DO UPDATE SET
                        success_count=s.success_count+excluded.success_count,
                        failed_count=s.failed_count+excluded.failed_count,
                        upstream_total_seconds=s.upstream_total_seconds+excluded.upstream_total_seconds,
                        upstream_timed_count=s.upstream_timed_count+excluded.upstream_timed_count,
                        gateway_total_seconds=s.gateway_total_seconds+excluded.gateway_total_seconds,
                        gateway_timed_count=s.gateway_timed_count+excluded.gateway_timed_count,
                        last_status=excluded.last_status, updated_at=excluded.updated_at
                """, mode, day, credential, model, int(success), int(not success),
                    float(upstream or 0), int(upstream is not None),
                    float(gateway or 0), int(gateway is not None), status)

    async def combined(self, mode, day, history, credentials):
        async with self.pool.acquire(timeout=4) as conn:
            increments = await conn.fetch("""
                SELECT credential_name,model_name,success_count,failed_count,last_status,updated_at,
                       upstream_total_seconds,upstream_timed_count,gateway_total_seconds,gateway_timed_count
                FROM model_stat_increments WHERE mode=$1 AND period=$2
            """, mode, day or '*')
        rows = {(r["credential_name"], r["model_name"]): r for r in history}
        for delta in increments:
            name = delta["credential_name"]
            key = (name, delta["model_name"])
            if key not in rows:
                email, tier = credentials.get(name, (None, None))
                rows[key] = dict(delta, user_email=email, tier=tier)
                continue
            row = rows[key]
            for field in COUNTERS:
                row[field] = (row[field] or 0) + delta[field]
            if delta["updated_at"] >= row["updated_at"]:
                row.update(last_status=delta["last_status"], updated_at=delta["updated_at"])
        return [rows[key] for key in sorted(rows)]
