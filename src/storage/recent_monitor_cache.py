"""Write-through Redis tail for recent realtime pages. The database certifies completeness."""
import asyncio
import hashlib
import json
import os
import time
from uuid import uuid4

from redis.exceptions import RedisError

from log import log
from src.redis_config import RedisConfigurationError, create_redis_client_from_env
from src.storage.main_postgres import PostgresDatabase, main_dsn
from src.storage.sqlite_pragmas import connect

LIMIT = 1000
FIELDS = "id, created_at, api_key_id, credential_name, success"
STATE = "monitor_tail_versions"
PG_SLOT = "(pg_backend_pid() % 32)"

# A single evictable key contains the version marker and at most 1000 records.
# All scores are zero: fixed-width IDs retain BIGINT order without double rounding.
_READ = """
local marker = redis.call('ZRANGE', KEYS[1], 0, 0)[1] or ''
if marker ~= ARGV[1] then return {0, marker} end
local rows = redis.call('ZRANGE', KEYS[1], 1, -1)
return {1, '[' .. table.concat(rows, ',') .. ']'}
"""
_UPDATE = """
local marker = redis.call('ZRANGE', KEYS[1], 0, 0)[1] or ''
local next
if ARGV[1] == 'seed' then
    if marker ~= ARGV[2] then return 0 end
    redis.call('DEL', KEYS[1])
    next = ARGV[3]
else
    if marker == '' then return 0 end
    local versions = cjson.decode(string.sub(marker, 2))
    local slot = tonumber(ARGV[2]) + 1
    if versions[slot] ~= ARGV[3] then return 0 end
    versions[slot] = ARGV[4]
    next = '!' .. cjson.encode(versions)
    redis.call('ZREM', KEYS[1], marker)
end
if #ARGV >= 5 then redis.call('ZADD', KEYS[1], unpack(ARGV, 5)) end
redis.call('ZREMRANGEBYRANK', KEYS[1], 0, -1001)
redis.call('ZADD', KEYS[1], 0, next)
redis.call('EXPIRE', KEYS[1], 86400)
return 1
"""


async def install(db, postgres):
    """Opt-in installation. Once present, triggers also cover non-Redis workers."""
    if not os.getenv("REDIS_URL", "").strip():
        return None
    exists_sql = (f"SELECT to_regclass('{STATE}')" if postgres else
                  f"SELECT name FROM sqlite_master WHERE type='table' AND name='{STATE}'")
    found = await (await db.execute(exists_sql)).fetchone()
    if not found or not found[0]:
        identity = uuid4().hex
        await db.execute(f"CREATE TABLE {STATE} (slot INTEGER PRIMARY KEY, version BIGINT NOT NULL, identity TEXT NOT NULL)")
        slots = 32 if postgres else 1
        # identity is an internally generated UUID, persisted across worker restarts.
        await db.execute(f"INSERT INTO {STATE} VALUES " + ",".join(f"({i},0,'{identity}')" for i in range(slots)))
        events = ["INSERT", "DELETE", "UPDATE OF id,created_at,api_key_id,credential_name,success"]
        if postgres:
            # A batch changes a single shard once. Independent writers need not
            # serialize on a global revision row, and rollback undoes the revision.
            await db.execute(f"""CREATE FUNCTION monitor_tail_changed() RETURNS trigger
                LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
                BEGIN UPDATE {STATE} SET version=version+1 WHERE slot={PG_SLOT}; RETURN NULL; END $$""")
            events.append("TRUNCATE")
        for event in events:
            name = "monitor_tail_" + event.split()[0].lower()
            trigger = f"CREATE TRIGGER {name} AFTER {event} ON call_records"
            if postgres:
                await db.execute(trigger + " FOR EACH STATEMENT EXECUTE FUNCTION monitor_tail_changed()")
            else:
                await db.execute(trigger + f" BEGIN UPDATE {STATE} SET version=version+1 WHERE slot=0; END")
    return (await (await db.execute(f"SELECT identity FROM {STATE} WHERE slot=0")).fetchone())[0]


def members(rows):
    args = []
    for record_id, stamp, key, name, success in rows:
        row = [f"{record_id:020d}", stamp, key, name, int(success)]
        args.extend((0, json.dumps(row, separators=(",", ":"), ensure_ascii=False)))
    return args


class RecentMonitorCache:
    def __init__(self, path, identity, excluded_sql):
        self.path = path
        self.postgres = isinstance(path, PostgresDatabase)
        self.excluded_sql = excluded_sql
        self.key = None
        if identity:
            location = main_dsn() + "|" + path.schema if self.postgres else os.path.realpath(path)
            digest = hashlib.sha256((location + "|" + identity).encode()).hexdigest()[:32]
            self.key = "gcli:monitor:recent:v1:" + digest
        self.client = None
        if self.key:
            try:
                self.client = create_redis_client_from_env(socket_timeout=.1, socket_connect_timeout=.1)
            except RedisConfigurationError:
                log.warning("Invalid recent monitor Redis configuration")
        self.retry_at = 0.
        self.queue = asyncio.Queue(maxsize=8)
        self.writer = None
        log.info("Recent monitor cache backend: " + ("Redis" if self.client else "main database"))

    @property
    def available(self):
        return self.client is not None and time.monotonic() >= self.retry_at

    async def command(self, script, *args):
        if self.available:
            try:
                async with asyncio.timeout(.15):
                    return await self.client.eval(script, 1, self.key, *args)
            except (RedisError, TimeoutError, OSError) as exc:
                if self.available:
                    log.warning(f"Recent monitor Redis unavailable: {type(exc).__name__}")
                self.retry_at = time.monotonic() + 3
        return None

    async def revision(self, db):
        slot = PG_SLOT if self.postgres else "0"
        return await (await db.execute(f"SELECT slot, version FROM {STATE} WHERE slot={slot}")).fetchone()

    def publish(self, revision, increment, rows):
        if not self.available or self.queue.full():
            # A missing revision forces a verified DB refill at the next read.
            return
        self.queue.put_nowait((revision, increment, rows))
        if self.writer is None or self.writer.done():
            self.writer = asyncio.create_task(self._drain())

    async def _drain(self):
        while not self.queue.empty():
            (slot, version), increment, rows = self.queue.get_nowait()
            try:
                if self.available:
                    result = await self.command(_UPDATE, "append", slot, str(version-increment), str(version), *members(rows))
                    if result == 0:
                        await self.read()  # Cold start, missed or out-of-order publication.
            except Exception as exc:
                log.warning(f"Recent monitor cache update skipped: {type(exc).__name__}")
            finally:
                self.queue.task_done()

    async def _state(self, db):
        sql = f"""SELECT slot, version, NULL FROM {STATE}
                  UNION ALL SELECT NULL, NULL, filename FROM ({self.excluded_sql}) AS excluded"""
        states = []
        banned = set()
        for slot, version, name in await (await db.execute(sql)).fetchall():
            if slot is None:
                banned.add(name)
            else:
                states.append((slot, str(version)))
        marker = "!" + json.dumps([v for _, v in sorted(states)], separators=(",", ":"))
        return marker, banned

    async def read(self):
        if not self.available:
            return None
        # No database connection is held during Redis I/O. Match every shard:
        # MAX(id) alone misses late commits with smaller IDs and in-place changes.
        async with connect(self.path, single_select=True) as db:
            marker, banned = await self._state(db)
        reply = await self.command(_READ, marker)
        if reply is None:
            return None
        hit, payload = reply
        if hit:
            try:
                return json.loads(payload), banned
            except (ValueError, TypeError):
                # Refill a malformed payload only if its generation still matches.
                payload = marker
        async with connect(self.path) as db:
            await db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY" if self.postgres else "BEGIN")
            marker, banned = await self._state(db)
            rows = await (await db.execute(f"SELECT {FIELDS} FROM call_records ORDER BY id DESC LIMIT {LIMIT}")).fetchall()
        # CAS avoids replacing another worker's intervening publication.
        await self.command(_UPDATE, "seed", payload, marker, "", *members(rows))
        return rows, banned

    async def selection(self, failed_only, api_key_id):
        snapshot = await self.read()
        if snapshot is None:
            return None
        rows, banned = snapshot
        accounts, keys = set(), set()
        logs = failed = account_logs = account_failed = 0
        matches = []
        for record_id, stamp, key, name, success in rows:
            if stamp < 0 or name in banned:
                continue
            logs += 1
            failed += not success
            keys.add(key)
            if name:
                accounts.add(name)
                account_logs += 1
                account_failed += not success
            if (not failed_only or not success) and (not api_key_id or key == api_key_id):
                matches.append((stamp, int(record_id)))
        matches.sort(reverse=True)
        counters = dict(logs=logs, failed=failed, accounts=len(accounts), keys=len(keys),
                        account_logs=account_logs, account_failed=account_failed)
        return [record_id for _, record_id in matches], counters

    async def close(self):
        if self.writer is not None:
            self.writer.cancel()
            await asyncio.gather(self.writer, return_exceptions=True)
        if self.client is not None:
            await self.client.aclose()
