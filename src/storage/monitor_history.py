"""Persistent five-minute monitor totals. Complete buckets never read call_records."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import math

from log import log
from src.storage.main_postgres import PostgresDatabase
from src.storage.sqlite_pragmas import connect

BUCKET = 300
LOCK_NAMESPACE = 72210502
DIMS = ("api_key_id", "credential_name", "model_name", "upstream_model", "success")
TOKENS = ("input_tokens", "output_tokens", "cache_tokens", "thought_tokens", "total_tokens")
SUMS = ("n", "gateway_n", "gateway_sum", *TOKENS, "cost_e8")
LABELS = ("currency", "task_type", "channel")
KEY = ("bucket", *DIMS)
FIELDS = (*KEY, *SUMS, *LABELS, "first_at", "last_at", "last_id", "last_code")
TABLE = "monitor_history_5m"
DAY = 86400
OFFSET = 8 * 3600
DAILY = "monitor_history_daily"
DIRTY = "monitor_history_rebuild"
DIRTY_DAY = "monitor_history_rebuild_day"
COLS = ", ".join(FIELDS)


def bucket_sql(ts):
    return f"(CAST(floor({ts} / {BUCKET}) AS BIGINT) * {BUCKET})"


def day_sql(ts):
    return f"(CAST(floor(({ts} + {OFFSET}) / {DAY}) AS BIGINT) * {DAY} - {OFFSET})"


def cost_sql(value):
    # Parse canonical non-negative eight-decimal text as integers; SQLite NUMERIC
    # would otherwise go through binary floating point before converting to E-8.
    return (f"CASE WHEN length({value}) >= 10 AND substr({value}, length({value})-8, 1)='.' "
            f"AND substr({value}, 1, 1) <> '-' AND upper({value}) NOT LIKE '%E%' THEN "
            f"CAST(substr({value}, 1, length({value})-9) AS BIGINT) * 100000000 "
            f"+ CAST(substr({value}, length({value})-7, 8) AS BIGINT) "
            f"ELSE CAST(round(CAST({value} AS NUMERIC) * 100000000) AS BIGINT) END")


def raw_projection(prefix, bucket=bucket_sql):
    def col(name):
        return prefix + name
    return ", ".join((
        bucket(col("created_at")), *map(col, DIMS), "1",
        f"CASE WHEN {col('gateway_seconds')} IS NULL THEN 0 ELSE 1 END",
        f"COALESCE({col('gateway_seconds')}, 0)", *map(col, TOKENS),
        cost_sql(col("total_cost")), *map(col, LABELS),
        col("created_at"), col("created_at"), col("id"), col("status_code"),
    ))


def upsert_updates(table):
    newer = f"(excluded.last_at, excluded.last_id) > ({table}.last_at, {table}.last_id)"
    updates = [f"{k} = {table}.{k} + excluded.{k}" for k in SUMS]
    updates += [f"{k} = CASE WHEN excluded.{k} > {table}.{k} THEN excluded.{k} ELSE {table}.{k} END" for k in LABELS]
    updates += [f"first_at = CASE WHEN excluded.first_at < {table}.first_at THEN excluded.first_at ELSE {table}.first_at END"]
    updates += [f"{k} = CASE WHEN {newer} THEN excluded.{k} ELSE {table}.{k} END" for k in ("last_at", "last_id", "last_code")]
    return f"ON CONFLICT ({', '.join(KEY)}) DO UPDATE SET " + ", ".join(updates)


def insert_one(table, bucket):
    return f"INSERT INTO {table} ({COLS}) VALUES ({raw_projection('NEW.', bucket)}) {upsert_updates(table)}"


def schema_sql(table):
    cols = ["bucket BIGINT NOT NULL", *(f"{k} TEXT NOT NULL" for k in DIMS[:-1]), "success BIGINT NOT NULL"]
    cols.extend(f"{k} {'DOUBLE PRECISION' if k == 'gateway_sum' else 'BIGINT'} NOT NULL" for k in SUMS)
    cols.extend(f"{k} TEXT NOT NULL" for k in LABELS)
    cols.extend(("first_at DOUBLE PRECISION NOT NULL", "last_at DOUBLE PRECISION NOT NULL",
                 "last_id BIGINT NOT NULL", "last_code BIGINT", f"PRIMARY KEY ({', '.join(KEY)})"))
    return f"CREATE TABLE {table} ({', '.join(cols)})"


def invalidate(ref):
    ts = ref + ".created_at"
    return "; ".join(f"INSERT INTO {table} (bucket) VALUES ({bucket(ts)}) ON CONFLICT (bucket) DO NOTHING"
                     for table, bucket in ((DIRTY, bucket_sql), (DIRTY_DAY, day_sql)))


async def install_pg_statement_trigger(db, event, transition):
    name = "monitor_history_" + event.lower()
    days = f"SELECT DISTINCT CAST(floor((created_at + {OFFSET}) / {DAY}) AS INTEGER) AS day FROM changed ORDER BY day"
    statements = [f"PERFORM pg_advisory_xact_lock_shared({LOCK_NAMESPACE}, day) FROM ({days}) AS days"]
    # Every batch acquires overlapping summary keys in the same order.
    for table, queue, bucket in ((DAILY, DIRTY_DAY, day_sql), (TABLE, DIRTY, bucket_sql)):
        if event == "INSERT":
            source = f"SELECT {raw_projection('r.', bucket)} FROM changed r"
            folded = fold_sql("SELECT * FROM weighted")
            statements.append(f"""INSERT INTO {table} ({COLS}) WITH weighted({COLS}) AS ({source})
                SELECT * FROM ({folded}) AS grouped ORDER BY {', '.join(KEY)} {upsert_updates(table)}""")
        else:
            statements.append(f"""INSERT INTO {queue} SELECT DISTINCT {bucket('created_at')}
                FROM changed ORDER BY 1 ON CONFLICT DO NOTHING""")
    body = "; ".join(statements)
    await db.execute(f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$ BEGIN {body}; RETURN NULL; END $$")
    await db.execute(f"""CREATE TRIGGER {name} AFTER {event} ON call_records
        REFERENCING {transition} TABLE AS changed FOR EACH STATEMENT EXECUTE FUNCTION {name}()""")


async def install(db, *, postgres):
    """Install once. Seed occupied time span via index endpoints, without scanning old rows."""
    if postgres:
        found = await (await db.execute(f"SELECT to_regclass('{TABLE}')")).fetchone()
        if found[0]:
            return
        # No writer can slip between trigger installation and capturing the old range.
        await db.execute("LOCK TABLE call_records IN SHARE ROW EXCLUSIVE MODE")
    else:
        found = await (await db.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{TABLE}'"
        )).fetchone()
        if found:
            return
    for table in (TABLE, DAILY):
        await db.execute(schema_sql(table))
    for queue in (DIRTY, DIRTY_DAY):
        await db.execute(f"CREATE TABLE {queue} (bucket BIGINT PRIMARY KEY)")
    watched = ", ".join(("id", "created_at", *DIMS, "gateway_seconds", *TOKENS, "total_cost", *LABELS, "status_code"))
    for event, action, refs in (
        ("INSERT", insert_one(TABLE, bucket_sql) + "; " + insert_one(DAILY, day_sql), ("NEW",)),
        ("DELETE", invalidate("OLD"), ("OLD",)),
        ("UPDATE OF " + watched, invalidate("OLD") + "; " + invalidate("NEW"), ("OLD", "NEW")),
    ):
        name = "monitor_history_" + event.split()[0].lower()
        if postgres and event in ("INSERT", "DELETE"):
            await install_pg_statement_trigger(db, event, "NEW" if event == "INSERT" else "OLD")
        elif postgres:
            locks = "; ".join(f"PERFORM pg_advisory_xact_lock_shared({LOCK_NAMESPACE}, "
                              f"CAST(floor(({ref}.created_at + {OFFSET}) / {DAY}) AS INTEGER))" for ref in refs)
            await db.execute(f"""CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
                BEGIN {locks}; {action}; RETURN NULL; END $$""")
            await db.execute(f"CREATE TRIGGER {name} AFTER {event} ON call_records FOR EACH ROW EXECUTE FUNCTION {name}()")
        else:
            await db.execute(f"CREATE TRIGGER {name} AFTER {event} ON call_records BEGIN {action}; END")
    # Sparse legacy histories: discover each occupied bucket with an index seek.
    await db.execute(f"""WITH RECURSIVE occupied(bucket) AS (
        SELECT {bucket_sql('(SELECT MIN(created_at) FROM call_records)')}
        UNION ALL SELECT {bucket_sql('(SELECT MIN(created_at) FROM call_records WHERE created_at >= occupied.bucket + 300)')}
        FROM occupied WHERE bucket IS NOT NULL)
        INSERT INTO {DIRTY} (bucket) SELECT bucket FROM occupied WHERE bucket IS NOT NULL""")
    await db.execute(f"INSERT INTO {DIRTY_DAY} SELECT DISTINCT {day_sql('bucket')} FROM {DIRTY}")


def fold_sql(source):
    """Fold weighted rows; latest status is ordered by timestamp and then persistent ID."""
    return f"""WITH ranked AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY {', '.join(KEY)}
               ORDER BY last_at DESC, last_id DESC) AS rn FROM ({source}) AS input
    ) SELECT {', '.join(KEY)}, {', '.join('SUM('+k+')' for k in SUMS)},
        {', '.join('MAX('+k+')' for k in LABELS)}, MIN(first_at), MAX(last_at),
        MAX(CASE WHEN rn=1 THEN last_id END), MAX(CASE WHEN rn=1 THEN last_code END)
        FROM ranked GROUP BY {', '.join(KEY)}"""


def raw_rows(predicate):
    return f"SELECT {raw_projection('r.')} FROM call_records r WHERE {predicate}"


class HistoryPreparing(Exception):
    pass


class MonitorHistory:
    def __init__(self, path):
        self.path = path
        self.postgres = isinstance(path, PostgresDatabase)
        self.worker = None

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    async def repair_one(self):
        async with connect(self.path) as db:
            if not self.postgres:
                await db.execute("BEGIN IMMEDIATE")
            latest = await (await db.execute(f"SELECT MAX(bucket) FROM {DIRTY_DAY}")).fetchone()
            day = latest[0]
            if day is None:
                return False
            if self.postgres:
                await db.execute("SELECT pg_advisory_xact_lock(?, ?)", (LOCK_NAMESPACE, (day + OFFSET) // DAY))
                pending = await (await db.execute(f"SELECT 1 FROM {DIRTY_DAY} WHERE bucket=?", (day,))).fetchone()
                if not pending:
                    return True
            fine = await (await db.execute(
                f"SELECT bucket FROM {DIRTY} WHERE bucket >= ? AND bucket < ? ORDER BY bucket LIMIT 8",
                (day, day + DAY),
            )).fetchall()
            if not fine:
                queue, table, bucket = DIRTY_DAY, DAILY, day
                source = f"SELECT {day_sql('bucket')}, {', '.join(FIELDS[1:])} FROM {TABLE} WHERE bucket >= ? AND bucket < ?"
                end = bucket + DAY
            else:
                queue, table, bucket = DIRTY, TABLE, fine[0][0]
                end = bucket
                for (start,) in fine:
                    if start != end:
                        break
                    end += BUCKET
                source = raw_rows("r.created_at >= ? AND r.created_at < ?")
            await db.execute(f"DELETE FROM {table} WHERE bucket >= ? AND bucket < ?", (bucket, end))
            folded = fold_sql("SELECT * FROM weighted")
            await db.execute(f"""INSERT INTO {table} ({COLS}) WITH weighted({COLS}) AS ({source})
                       SELECT * FROM ({folded}) AS folded""", (bucket, end))
            await db.execute(f"DELETE FROM {queue} WHERE bucket >= ? AND bucket < ?", (bucket, end))
            await db.commit()
            return True

    async def rebuild(self):
        while await self.repair_one():
            await asyncio.sleep(0.01)

    async def _background(self):
        try:
            await self.rebuild()
        except Exception as exc:
            log.error(f"Historical call monitor backfill failed: {type(exc).__name__}")

    @asynccontextmanager
    async def snapshot(self, since):
        async with connect(self.path) as db:
            await db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                             if self.postgres else "BEGIN")
            start_day = math.floor((since + OFFSET) / DAY) * DAY - OFFSET
            if await (await db.execute(f"SELECT 1 FROM {DIRTY_DAY} WHERE bucket >= ? LIMIT 1", (start_day,))).fetchone():
                if self.worker is None or self.worker.done():
                    self.worker = asyncio.create_task(self._background())
                raise HistoryPreparing()
            yield db

    @staticmethod
    def weights(since, *, day_end=None):
        """Use daily totals, five-minute totals on the cut day, then one indexed edge."""
        parts, args = [], []
        next_five = math.ceil(since / BUCKET) * BUCKET
        next_day = math.ceil((since + OFFSET) / DAY) * DAY - OFFSET
        if since < next_five:
            parts.append(raw_rows("r.created_at >= ? AND r.created_at < ?"))
            args.extend((since, next_five))
        if day_end is not None:
            # Pagination is already narrowed to a single calendar day.
            parts.append(f"SELECT {COLS} FROM {TABLE} WHERE bucket >= ? AND bucket < ?")
            args.extend((next_five, day_end))
        else:
            if next_five < next_day:
                parts.append(f"SELECT {COLS} FROM {TABLE} WHERE bucket >= ? AND bucket < ?")
                args.extend((next_five, next_day))
            parts.append(f"SELECT {COLS} FROM {DAILY} WHERE bucket >= ?")
            args.append(next_day)
        return f"WITH weights({COLS}) AS ({' UNION ALL '.join(parts)})", args

    async def overview(self, since, view, failed_only, visible):
        weights, args = self.weights(since)
        # Keep the compact dimension combinations in SQL. Only the groups needed
        # for this view cross the database connection.
        dims = ("credential_name", "model_name") if view == "account" else DIMS[:4]
        group = ", ".join(dims)
        where = visible
        if failed_only:
            where += " AND success = 0"
        if view == "account":
            where += " AND credential_name <> ''"
        async with self.snapshot(since) as db:
            counters = await self.counters(db, weights, args, visible)
            oldest = None
            if since == 0:
                oldest = (await (await db.execute(f"""SELECT MIN(first_at) FROM {DAILY}
                    WHERE bucket=(SELECT MIN(bucket) FROM {DAILY})""")).fetchone())[0]
            async with db.execute(f"""{weights}, ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY {group} ORDER BY last_at DESC, last_id DESC) AS rn
                FROM weights WHERE {where}
            ) SELECT {group}, {', '.join('SUM('+k+')' for k in SUMS)},
                     SUM(n * success), {', '.join('MAX('+k+')' for k in LABELS)},
                     MAX(last_at), MAX(CASE WHEN rn=1 THEN last_id END),
                     MAX(CASE WHEN rn=1 THEN success END), MAX(CASE WHEN rn=1 THEN last_code END)
              FROM ranked GROUP BY {group}""", args) as cursor:
                records = await cursor.fetchall()
        names = (*dims, *SUMS, "success_count", *LABELS, "last_at", "last_id", "last_success", "last_status_code")
        rows = self.format_groups(view, [dict(zip(names, row)) for row in records])
        return rows, counters, oldest

    @staticmethod
    def format_groups(view, records):
        groups = {}
        for r in records:
            key = ((r["credential_name"], r["model_name"]) if view == "account"
                   else (r["api_key_id"], r["credential_name"]))
            if key not in groups:
                groups[key] = {**r, **dict.fromkeys((*SUMS, "success_count"), 0),
                               "group_key": key[0], "models": {}, "upstreams": {}}
            item = groups[key]
            for field in (*SUMS, "success_count"):
                item[field] += r[field]
            for field in LABELS:
                item[field] = max(item[field], r[field])
            if (r["last_at"], r["last_id"]) >= (item["last_at"], item["last_id"]):
                for field in ("last_at", "last_id", "last_success", "last_status_code"):
                    item[field] = r[field]
            if view == "key":
                for field, tally in (("model_name", "models"), ("upstream_model", "upstreams")):
                    name = r[field]
                    if name:
                        item[tally][name] = item[tally].get(name, 0) + r["n"]
        rows = []
        for key, item in groups.items():
            item["total"] = int(item.pop("n"))
            item["success_count"] = int(item["success_count"])
            item["failed_count"] = item["total"] - item["success_count"]
            item["success_rate"] = round(100 * item["success_count"] / item["total"], 1)
            gateway_n, gateway_sum = item.pop("gateway_n"), item.pop("gateway_sum")
            item["avg_gateway_seconds"] = float(gateway_sum) / int(gateway_n) if gateway_n else 0.0
            item["total_cost"] = format(Decimal(item.pop("cost_e8")).scaleb(-8), ".8f")
            item["last_called_at"] = item.pop("last_at")
            item.pop("last_id")
            item["last_success"] = bool(item["last_success"])
            for field in TOKENS:
                item[field] = int(item[field])
            for tally, label, calls in (("models", "top_model", "top_model_calls"),
                                        ("upstreams", "top_upstream_model", "top_upstream_calls")):
                counts = item.pop(tally)
                if view == "key":
                    top = min(counts, key=lambda name: (-counts[name], name)) if counts else ""
                    item[label], item[calls] = top, int(counts.get(top, 0))
            if view == "key":
                item.pop("model_name")
                item.pop("upstream_model")
            rows.append(item)
        return sorted(rows, key=lambda r: (-r["total"], r["group_key"],
                                          r.get("model_name", r["credential_name"])))

    @staticmethod
    async def counters(db, weights, args, visible):
        async with db.execute(f"""{weights} SELECT SUM(n), SUM(n*(1-success)),
            COUNT(DISTINCT NULLIF(credential_name,'')), COUNT(DISTINCT api_key_id),
            SUM(CASE WHEN credential_name<>'' THEN n ELSE 0 END),
            SUM(CASE WHEN credential_name<>'' THEN n*(1-success) ELSE 0 END)
            FROM weights WHERE {visible}""", args) as cursor:
            values = await cursor.fetchone()
        keys = ("logs", "failed", "accounts", "keys", "account_logs", "account_failed")
        return {k: int(v or 0) for k, v in zip(keys, values)}

    @staticmethod
    def page_slices(counts, offset, needed):
        for start, count in counts:
            count = int(count)
            if offset >= count:
                offset -= count
                continue
            take = min(count - offset, needed)
            yield int(start), offset, take
            needed -= take
            if not needed:
                break
            offset = 0

    async def page(self, backend, since, page, page_size, failed_only, api_key_id, visible):
        weights, args = self.weights(since)
        page_size = min(max(int(page_size), 1), 200)
        page = max(int(page), 1)
        condition = visible
        extra_args = []
        if failed_only:
            condition += " AND success=0"
        if api_key_id:
            condition += " AND api_key_id=?"
            extra_args.append(api_key_id)
        async with self.snapshot(since) as db:
            counters = await self.counters(db, weights, args, visible)
            async with db.execute(f"""{weights} SELECT {day_sql('first_at')}, SUM(n) FROM weights
                WHERE {condition} GROUP BY 1 ORDER BY 1 DESC""", args + extra_args) as cursor:
                days = await cursor.fetchall()
            records = []
            for day, offset, take in self.page_slices(days, (page - 1) * page_size, page_size):
                start = max(day, since)
                weights, args = self.weights(start, day_end=day + DAY)
                async with db.execute(f"""{weights} SELECT {bucket_sql('first_at')}, SUM(n)
                    FROM weights WHERE {condition} GROUP BY 1 ORDER BY 1 DESC""", args + extra_args) as cursor:
                    buckets = await cursor.fetchall()
                for bucket, skip, limit in self.page_slices(buckets, offset, take):
                    where, params = backend._call_record_filters(max(start, bucket), bucket + BUCKET, failed_only, api_key_id)
                    fields = ", ".join(backend._CALL_RECORD_COLUMNS)
                    async with db.execute(f"""SELECT {fields} FROM call_records WHERE {where}
                        ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""", [*params, limit, skip]) as cursor:
                        records.extend(backend._call_record_row(row) for row in await cursor.fetchall())
        return dict(records=records, counters=counters, total=sum(int(n) for _, n in days),
                    page=page, page_size=page_size, degraded=False)
