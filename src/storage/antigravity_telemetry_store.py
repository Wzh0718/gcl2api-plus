"""Account-owned telemetry settings; no tokens or event bodies in this table."""
import hashlib
import json
import os
import time
import uuid

import psycopg
import aiosqlite

from src.storage.sqlite_pragmas import connect as sqlite_connect


def subject_ref(account):
    email = (account.get("user_email") or "").strip().lower()
    return hashlib.sha256(email.encode()).hexdigest() if email else ""


class AntigravityTelemetryStore:
    async def _create_telemetry_tables(self, db):
        await db.execute("""CREATE TABLE IF NOT EXISTS antigravity_telemetry_profiles (
            account_id INTEGER PRIMARY KEY,
            subject_ref TEXT NOT NULL UNIQUE,
            installation_id TEXT NOT NULL UNIQUE,
            mode TEXT NOT NULL DEFAULT 'off' CHECK(mode IN ('off','preview','send')),
            policy_revision TEXT NOT NULL,
            environment_json TEXT NOT NULL,
            last_outcome TEXT NOT NULL DEFAULT '',
            last_http_status INTEGER,
            last_event_at REAL,
            updated_at REAL NOT NULL
        )""")
        await db.execute("""CREATE TRIGGER IF NOT EXISTS delete_account_telemetry
            AFTER DELETE ON antigravity_credentials BEGIN
                DELETE FROM antigravity_telemetry_profiles WHERE account_id = OLD.id;
            END""")
        await db.execute("""CREATE TABLE IF NOT EXISTS antigravity_telemetry_events (
            event_id TEXT PRIMARY KEY, timing_id TEXT NOT NULL,
            account_id INTEGER NOT NULL, filename TEXT NOT NULL,
            mode TEXT NOT NULL, outcome TEXT NOT NULL, http_status INTEGER,
            attempts INTEGER NOT NULL, event_json TEXT NOT NULL,
            binding_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS telemetry_events_created ON antigravity_telemetry_events(created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS telemetry_events_timing ON antigravity_telemetry_events(timing_id)")
        await db.execute("""CREATE TRIGGER IF NOT EXISTS delete_account_telemetry_events
            AFTER DELETE ON antigravity_credentials BEGIN
                DELETE FROM antigravity_telemetry_events WHERE account_id = OLD.id;
            END""")

    async def save_telemetry_event(self, snapshot, event, outcome, status=None, attempts=0):
        """Persist only allowlisted metadata, never bearer tokens or proxy URLs."""
        self._ensure_initialized()
        now = time.time()
        binding = {"installation_id": snapshot.installation_id,
                   "environment_ref": snapshot.environment, "user_agent": snapshot.user_agent,
                   "route_ref": snapshot.route_ref, "native_transport": snapshot.native_transport}
        async with sqlite_connect(self._db_path) as db:
            # Do not recreate event history after its owning account was deleted.
            await db.execute("""INSERT INTO antigravity_telemetry_events
                SELECT ?,?,?,?,?,?,?,?,?,?,?,? WHERE EXISTS
                    (SELECT 1 FROM antigravity_credentials WHERE id=? AND filename=?)
                ON CONFLICT(event_id) DO UPDATE SET outcome=excluded.outcome,
                    http_status=excluded.http_status, attempts=excluded.attempts, updated_at=excluded.updated_at,
                    event_json=excluded.event_json""",
                (event["event_id"], snapshot.timing_id, snapshot.account_id, snapshot.filename,
                 snapshot.mode, outcome, status, attempts, json.dumps(event), json.dumps(binding),
                 event.get("_delivery", {}).get("enqueued_at", now), now, snapshot.account_id, snapshot.filename))
            await db.execute("DELETE FROM antigravity_telemetry_events WHERE created_at < ?", (now - 7 * 86400,))
            await db.execute("""DELETE FROM antigravity_telemetry_events WHERE event_id IN
                (SELECT event_id FROM antigravity_telemetry_events ORDER BY created_at DESC LIMIT -1 OFFSET 10000)""")
            await db.commit()

    async def list_telemetry_events(self, timing_ids):
        self._ensure_initialized()
        ids = list(dict.fromkeys(value for value in timing_ids if value))[:200]
        if not ids:
            return []
        async with sqlite_connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"""SELECT * FROM antigravity_telemetry_events
                WHERE timing_id IN ({','.join('?' for _ in ids)}) AND created_at >= ?
                ORDER BY created_at DESC LIMIT 2000""", [*ids, time.time() - 7 * 86400]) as cursor:
                rows = await cursor.fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["event"] = json.loads(item.pop("event_json"))
            item["binding"] = json.loads(item.pop("binding_json"))
            result.append(item)
        return result

    async def get_telemetry_account(self, filename=None, *, access_token=None):
        self._ensure_initialized()
        if filename is not None:
            where, params = "a.filename = ?", (os.path.basename(filename),)
        elif access_token:
            where = """p.mode IN ('preview','send') AND
                (json_extract(a.credential_data,'$.access_token') = ? OR
                 json_extract(a.credential_data,'$.token') = ?)"""
            params = (access_token, access_token)
        else:
            return None
        async with sqlite_connect(self._db_path, single_select=True) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"""SELECT a.id account_id, a.filename, a.user_email,
                a.disabled, a.credential_data, p.subject_ref, p.installation_id, p.mode,
                p.policy_revision, p.environment_json, p.last_outcome,
                p.last_http_status, p.last_event_at
                FROM antigravity_credentials a LEFT JOIN antigravity_telemetry_profiles p
                ON p.account_id = a.id WHERE {where} LIMIT 2""", params) as cursor:
                rows = await cursor.fetchall()
        # An ambiguous bearer must never select a different account as a fallback.
        if len(rows) != 1:
            return None
        account = dict(rows[0])
        account["credential_data"] = json.loads(account["credential_data"])
        account["environment"] = json.loads(account.pop("environment_json") or "{}")
        return account

    async def save_telemetry_identity(self, expected, email):
        """Bind userinfo only to the unchanged credential that supplied it."""
        self._ensure_initialized()
        async with sqlite_connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT credential_data FROM antigravity_credentials WHERE id=? AND filename=? AND disabled=0 AND COALESCE(user_email,'')=''",
                                  (expected["account_id"], expected["filename"])) as cursor:
                row = await cursor.fetchone()
            if row and json.loads(row[0]) == expected["credential_data"]:
                await db.execute("UPDATE antigravity_credentials SET user_email=? WHERE id=?", (email, expected["account_id"]))
            await db.commit()

    async def configure_account_telemetry(self, filename, expected, mode, environment):
        self._ensure_initialized()
        if mode not in {"off", "preview", "send"}:
            raise ValueError("遥测模式必须是 off、preview 或 send")
        subject = subject_ref(expected)
        if mode != "off" and (not subject or expected["disabled"]):
            raise ValueError("账号身份尚未就绪或账号已停用，暂不能配置遥测")
        async with sqlite_connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT id,user_email,disabled,credential_data FROM antigravity_credentials WHERE filename=?",
                                  (os.path.basename(filename),)) as cursor:
                row = await cursor.fetchone()
            if (not row or row[0] != expected["account_id"] or
                    row[1] != expected["user_email"] or row[2] != expected["disabled"] or
                    json.loads(row[3]) != expected["credential_data"]):
                raise ValueError("账号授权已变化，请重新加载后保存")
            async with db.execute("SELECT subject_ref,installation_id,environment_json,policy_revision FROM antigravity_telemetry_profiles WHERE account_id=?",
                                  (row[0],)) as cursor:
                old = await cursor.fetchone()
            if (old[3] if old else None) != expected.get("policy_revision"):
                raise ValueError("遥测设置已变化，请重新加载")
            if mode == "off" and not old:
                await db.commit()
                return
            # Reauthorizing another subject or changing the route creates a new environment.
            same = old and old[0] == subject and json.loads(old[2]) == environment
            installation = old[1] if same else str(uuid.uuid4())
            try:
                await db.execute("""INSERT INTO antigravity_telemetry_profiles
                    (account_id,subject_ref,installation_id,mode,policy_revision,environment_json,updated_at)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET
                    subject_ref=excluded.subject_ref,installation_id=excluded.installation_id,
                    mode=excluded.mode,policy_revision=excluded.policy_revision,
                    environment_json=excluded.environment_json,last_outcome='',last_http_status=NULL,
                    updated_at=excluded.updated_at""",
                    (row[0], subject or old[0], installation, mode, uuid.uuid4().hex,
                     json.dumps(environment, sort_keys=True), time.time()))
            except (aiosqlite.IntegrityError, psycopg.IntegrityError):
                raise ValueError("同一 Google 账号已绑定另一份遥测环境，请先处理重复凭证") from None
            await db.commit()

    async def record_telemetry_outcome(self, account_id, policy_revision, outcome, status=None):
        self._ensure_initialized()
        async with sqlite_connect(self._db_path) as db:
            await db.execute("""UPDATE antigravity_telemetry_profiles
                SET last_outcome=?,last_http_status=?,last_event_at=?
                WHERE account_id=? AND policy_revision=?""",
                (outcome, status, time.time(), account_id, policy_revision))
            await db.commit()
