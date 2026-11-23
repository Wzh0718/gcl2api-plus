import hashlib
import json
import time
from uuid import uuid4

import aiosqlite

from src.storage.sqlite_pragmas import connect as sqlite_connect

from src.api_keys import ApiKeyCipher


class ImportQueueCipher(ApiKeyCipher):
    KEY_FILENAME = "refresh_import.fernet.key"


class RefreshImportQueue:
    def __init__(self, db_path: str, credentials_dir: str):
        self.db_path = db_path
        self.credentials_dir = credentials_dir

    @staticmethod
    async def create_schema(db):
        await db.execute("""
            CREATE TABLE IF NOT EXISTS refresh_import_items (
                id INTEGER PRIMARY KEY,
                batch_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                payload_ciphertext TEXT,
                token_digest TEXT,
                proxy_group_id INTEGER,
                has_custom_proxy INTEGER NOT NULL DEFAULT 0,
                file_path TEXT,
                email TEXT,
                egress_ip TEXT,
                lease_id TEXT,
                lease_until INTEGER,
                retry_count INTEGER NOT NULL DEFAULT 0,
                retry_after INTEGER,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                UNIQUE(batch_id, position)
            )
        """)
        # Older databases predate deferred egress retries; add the columns in place.
        async with db.execute("PRAGMA table_info(refresh_import_items)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        for name, definition in (
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ("retry_after", "INTEGER"),
        ):
            if name not in existing:
                await db.execute(f"ALTER TABLE refresh_import_items ADD COLUMN {name} {definition}")
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_refresh_import_ready
                ON refresh_import_items(status, id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_refresh_import_token
                ON refresh_import_items(token_digest) WHERE status = 'imported'
        """)

    def _cipher(self):
        return ImportQueueCipher(self.credentials_dir)

    @staticmethod
    def _digest(token):
        return hashlib.sha256(token.encode()).hexdigest() if token else None

    async def create_batch(self, tokens, group_id, proxy_url):
        batch_id = uuid4().hex
        cipher = self._cipher()
        async with sqlite_connect(self.db_path) as db:
            await db.executemany(
                """INSERT INTO refresh_import_items
                   (batch_id, position, status, message, payload_ciphertext, token_digest,
                    proxy_group_id, has_custom_proxy)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        batch_id, position, "queued" if token else "failed",
                        None if token else "缺少或无效的 refresh_token",
                        cipher.encrypt(json.dumps({"refresh_token": token, "proxy_url": proxy_url})),
                        self._digest(token), group_id, bool(proxy_url),
                    )
                    for position, token in enumerate(tokens, start=1)
                ],
            )
            await db.commit()
        return batch_id

    async def list_batches(self):
        async with sqlite_connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT batch_id, MIN(created_at) AS created_at, COUNT(*) AS total
                   FROM refresh_import_items GROUP BY batch_id ORDER BY MAX(id) DESC"""
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def list_items(self, batch_id):
        async with sqlite_connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT id, position, status, message, proxy_group_id, has_custom_proxy,
                          file_path, email, egress_ip
                   FROM refresh_import_items WHERE batch_id = ? ORDER BY position""",
                (batch_id,),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def edit_item(self, item_id, refresh_token, mode, proxy_group_id, proxy_url):
        async with sqlite_connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                """SELECT payload_ciphertext FROM refresh_import_items
                   WHERE id = ? AND status IN ('queued', 'disabled', 'failed')""",
                (item_id,),
            ) as cursor:
                item = await cursor.fetchone()
            if not item:
                return False

            cipher = self._cipher()
            try:
                current = json.loads(cipher.decrypt(item[0]))
            except ValueError:
                current = {}
            token = refresh_token or current.get("refresh_token")
            if not token:
                raise ValueError("请输入新的 refresh_token")
            custom = (proxy_url or current.get("proxy_url")) if mode == "custom" else None
            if mode == "custom" and not custom:
                raise ValueError("请填写自定义代理地址")
            await db.execute(
                """UPDATE refresh_import_items SET
                       payload_ciphertext = ?, token_digest = ?, proxy_group_id = ?,
                       has_custom_proxy = ?, status = 'queued', message = NULL,
                       retry_count = 0, retry_after = NULL
                   WHERE id = ?""",
                (
                    cipher.encrypt(json.dumps({"refresh_token": token, "proxy_url": custom})),
                    self._digest(token), proxy_group_id, bool(custom), item_id,
                ),
            )
            await db.commit()
            return True

    async def enable_item(self, item_id):
        async with sqlite_connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE refresh_import_items SET
                       status = 'queued', message = NULL, retry_count = 0, retry_after = NULL
                   WHERE id = ? AND status = 'disabled'""",
                (item_id,),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def claim_next(self):
        async with sqlite_connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            from src.storage.main_mysql import MySQLDatabase
            if isinstance(self.db_path, MySQLDatabase):
                row = await (await db.execute("""
                    SELECT id,payload_ciphertext,token_digest,proxy_group_id,retry_count FROM refresh_import_items
                    WHERE (status='queued' OR (status='running' AND lease_until<=unixepoch()))
                      AND (retry_after IS NULL OR retry_after<=unixepoch())
                      AND NOT EXISTS (SELECT 1 FROM refresh_import_items WHERE status='running' AND lease_until>unixepoch())
                    ORDER BY id LIMIT 1 FOR UPDATE""")).fetchone()
                if row:
                    row["lease_id"] = uuid4().hex
                    await db.execute("UPDATE refresh_import_items SET status='running',message=NULL,lease_id=?,"
                                     "lease_until=unixepoch()+240 WHERE id=?", (row["lease_id"], row["id"]))
                await db.commit()
                return row
            async with db.execute(
                """UPDATE refresh_import_items
                   SET status = 'running', message = NULL, lease_id = ?, lease_until = unixepoch() + 240
                   WHERE id = (
                       SELECT id FROM refresh_import_items
                       WHERE (status = 'queued'
                              OR (status = 'running' AND lease_until <= unixepoch()))
                         AND (retry_after IS NULL OR retry_after <= unixepoch())
                       ORDER BY id LIMIT 1
                   ) AND NOT EXISTS (
                       SELECT 1 FROM refresh_import_items
                       WHERE status = 'running' AND lease_until > unixepoch()
                   )
                   RETURNING id, payload_ciphertext, token_digest, proxy_group_id, lease_id, retry_count""",
                (uuid4().hex,),
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
            return dict(row) if row else None

    def read_claim_payload(self, claim):
        return json.loads(self._cipher().decrypt(claim["payload_ciphertext"]))

    async def find_existing(self, token, digest):
        async with sqlite_connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT a.filename AS file_path, a.user_email AS email, a.fixed_proxy_ip AS egress_ip
                   FROM antigravity_credentials a
                   WHERE json_extract(a.credential_data, '$.refresh_token') = ?
                      OR a.filename IN (
                          SELECT file_path FROM refresh_import_items
                          WHERE token_digest = ? AND status = 'imported'
                      )
                   LIMIT 1""",
                (token, digest),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    @staticmethod
    async def finish_success(db, item_id, lease_id, filename, email, egress_ip):
        cursor = await db.execute(
            """UPDATE refresh_import_items SET
                   status = 'imported', message = NULL, file_path = ?, email = ?, egress_ip = ?,
                   payload_ciphertext = NULL, lease_id = NULL, lease_until = NULL
               WHERE id = ? AND status = 'running' AND lease_id = ? AND lease_until > unixepoch()""",
            (filename, email, egress_ip, item_id, lease_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("导入任务预留已失效")

    async def finish(self, claim, status, message=None, existing=None):
        async with sqlite_connect(self.db_path) as db:
            if existing is not None:
                await self.finish_success(
                    db, claim["id"], claim["lease_id"], existing["file_path"],
                    existing["email"], existing["egress_ip"],
                )
            else:
                await db.execute(
                    """UPDATE refresh_import_items SET
                           status = ?, message = ?, lease_id = NULL, lease_until = NULL
                       WHERE id = ? AND status = 'running' AND lease_id = ?""",
                    (status, message, claim["id"], claim["lease_id"]),
                )
            await db.commit()

    async def defer(self, claim, message, delay_seconds=30, max_retries=10):
        """Requeue an item whose egress probe was throttled instead of disabling it.

        A transient 429 from the egress-IP provider says nothing about the
        account, so the item stays queued and becomes claimable again after
        `delay_seconds`. Only an exhausted retry budget turns it into a
        disabled item, and the message keeps the real reason.
        """
        retries = int(claim.get("retry_count") or 0) + 1
        exhausted = retries >= int(max_retries)
        async with sqlite_connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE refresh_import_items SET
                       status = ?, message = ?, retry_count = ?, retry_after = ?,
                       lease_id = NULL, lease_until = NULL
                   WHERE id = ? AND status = 'running' AND lease_id = ?""",
                (
                    "disabled" if exhausted else "queued",
                    f"{message}（已重试 {retries} 次，请稍后手动启用）" if exhausted else message,
                    retries,
                    None if exhausted else int(time.time()) + int(delay_seconds),
                    claim["id"], claim["lease_id"],
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def disable(self, claim, message):
        async with sqlite_connect(self.db_path) as db:
            cursor = await db.execute(
                """UPDATE refresh_import_items SET
                       status = 'disabled', message = ?, lease_id = NULL, lease_until = NULL
                   WHERE id = ? AND status = 'running' AND lease_id = ?""",
                (message, claim["id"], claim["lease_id"]),
            )
            await db.commit()
            return cursor.rowcount == 1
