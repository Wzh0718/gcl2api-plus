"""SQLite persistence for Antigravity dynamic warmup state and leases."""

import json
import os
import time
from typing import Any, Dict, List, Optional

import aiosqlite


class AntigravityWarmupStore:
    @staticmethod
    def _default_warmup_state(filename: str) -> Dict[str, Any]:
        from src.antigravity_dynamic_warmup import (
            DEFAULT_WARMUP_CONFIG,
            normalize_warmup_config,
            warmup_selectable,
        )

        config = normalize_warmup_config(DEFAULT_WARMUP_CONFIG)
        return {
            "credential_name": filename,
            "status": "off",
            "level": 0,
            "success_count": 0,
            "window_started_at": None,
            "next_start_at": 0.0,
            "config": config,
            "selectable": warmup_selectable("off"),
        }

    async def get_antigravity_warmup(self, filename: str) -> Dict[str, Any]:
        self._ensure_initialized()
        filename = os.path.basename(filename)
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT status, level, success_count, window_started_at,
                          next_start_at, config_json
                   FROM antigravity_warmup_accounts WHERE credential_name = ?""",
                (filename,),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return self._default_warmup_state(filename)
        from src.antigravity_dynamic_warmup import (
            normalize_warmup_config,
            warmup_limits,
            warmup_selectable,
        )

        config = normalize_warmup_config(json.loads(row[5] or "{}"))
        return {
            "credential_name": filename,
            "status": row[0],
            "level": row[1],
            "success_count": row[2],
            "window_started_at": row[3],
            "next_start_at": row[4],
            "config": config,
            "limits": warmup_limits(config, row[1]),
            "selectable": warmup_selectable(row[0]),
        }

    async def set_antigravity_warmup(
        self,
        filenames: List[str],
        action: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        self._ensure_initialized()
        from src.antigravity_dynamic_warmup import normalize_warmup_config

        names = [os.path.basename(str(name)) for name in filenames]
        if not names:
            raise ValueError("至少选择一个养号账号")
        if action not in {"start", "cancel", "resume", "complete", "restart", "forbid"}:
            raise ValueError(f"不支持的养号操作: {action}")
        now = time.time()
        defaults = await self.get_config("antigravity_warmup_defaults", {})
        results: Dict[str, Dict[str, Any]] = {}
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            placeholders = ",".join("?" for _ in names)
            async with db.execute(
                f"SELECT filename FROM antigravity_credentials WHERE filename IN ({placeholders})",
                names,
            ) as cursor:
                existing = {row[0] for row in await cursor.fetchall()}
            missing = set(names) - existing
            if missing:
                for name in names:
                    if name in missing:
                        results[name] = {"status": "missing", "changed": False}
                    else:
                        results[name] = {
                            "status": "ok",
                            "changed": False,
                            "state": self._default_warmup_state(name),
                        }
                return results

            await db.execute("BEGIN IMMEDIATE")
            try:
                for name in names:
                    async with db.execute(
                        """SELECT status, config_json FROM antigravity_warmup_accounts
                           WHERE credential_name = ?""",
                        (name,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    previous_status = row[0] if row else "off"
                    previous_config = json.loads(row[1] or "{}") if row else {}
                    status = previous_status
                    changed = False
                    level = 0
                    success_count = 0
                    window_started_at = now
                    effective_config = previous_config
                    if action in {"start", "restart"}:
                        status = "warming"
                        effective_config = normalize_warmup_config(
                            config, base=previous_config or defaults
                        )
                        changed = True
                    elif action == "cancel":
                        if previous_status in {"warming", "paused"}:
                            status = "cancelled"
                            changed = True
                        else:
                            async with db.execute(
                                """SELECT level, success_count, window_started_at
                                   FROM antigravity_warmup_accounts
                                   WHERE credential_name = ?""",
                                (name,),
                            ) as cursor:
                                progress = await cursor.fetchone()
                            if progress:
                                level, success_count, window_started_at = progress
                    elif action == "resume":
                        if previous_status in {"cancelled", "paused"}:
                            status = "warming"
                            changed = True
                        async with db.execute(
                            """SELECT level, success_count, window_started_at
                               FROM antigravity_warmup_accounts
                               WHERE credential_name = ?""",
                            (name,),
                        ) as cursor:
                            progress = await cursor.fetchone()
                        if progress:
                            level, success_count, window_started_at = progress
                    elif action == "complete":
                        if previous_status in {"warming", "paused"}:
                            status = "active"
                            changed = True
                        async with db.execute(
                            """SELECT level, success_count, window_started_at
                               FROM antigravity_warmup_accounts
                               WHERE credential_name = ?""",
                            (name,),
                        ) as cursor:
                            progress = await cursor.fetchone()
                        if progress:
                            level, success_count, window_started_at = progress
                    elif action == "forbid":
                        status = "account_forbidden"
                        changed = previous_status != status

                    effective_config = normalize_warmup_config(effective_config)
                    await db.execute(
                        """INSERT INTO antigravity_warmup_accounts (
                               credential_name, status, level, success_count,
                               window_started_at, config_json, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(credential_name) DO UPDATE SET
                               status = excluded.status,
                               level = excluded.level,
                               success_count = excluded.success_count,
                               window_started_at = excluded.window_started_at,
                               config_json = excluded.config_json,
                               updated_at = excluded.updated_at""",
                        (
                            name,
                            status,
                            level,
                            success_count,
                            window_started_at,
                            json.dumps(effective_config),
                            now,
                        ),
                    )
                    results[name] = {
                        "status": "ok" if changed else "already_inactive",
                        "changed": changed,
                    }
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        for name in names:
            results[name]["state"] = await self.get_antigravity_warmup(name)
        return results

    async def acquire_antigravity_warmup_lease(
        self,
        filename: str,
        ip_key: str,
        *,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        from src.antigravity_dynamic_warmup import (
            normalize_warmup_config,
            warmup_limits,
            warmup_selectable,
        )

        filename = os.path.basename(filename)
        now = time.time() if now is None else float(now)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """SELECT status, level, next_start_at, config_json
                       FROM antigravity_warmup_accounts WHERE credential_name = ?""",
                    (filename,),
                ) as cursor:
                    account = await cursor.fetchone()
                if not account or account[0] in {"off", "active"}:
                    await db.commit()
                    return {"enabled": False, "acquired": True, "lease_id": None}
                if not warmup_selectable(account[0]):
                    await db.commit()
                    return {
                        "enabled": True,
                        "acquired": False,
                        "reason": account[0],
                        "retry_after": 0,
                    }

                config = normalize_warmup_config(json.loads(account[3] or "{}"))
                limits = warmup_limits(config, account[1])
                await db.execute(
                    "DELETE FROM antigravity_warmup_leases WHERE expires_at <= ?",
                    (now,),
                )
                await db.execute(
                    """INSERT INTO antigravity_warmup_ip_groups (ip_key, config_json)
                       VALUES (?, ?) ON CONFLICT(ip_key) DO NOTHING""",
                    (ip_key, json.dumps(config)),
                )
                async with db.execute(
                    """SELECT level, next_start_at, config_json
                       FROM antigravity_warmup_ip_groups WHERE ip_key = ?""",
                    (ip_key,),
                ) as cursor:
                    group = await cursor.fetchone()
                group_config = normalize_warmup_config(json.loads(group[2] or "{}"))
                group_limits = warmup_limits(group_config, group[0])

                async with db.execute(
                    """SELECT
                           (SELECT COUNT(*) FROM antigravity_warmup_leases
                            WHERE credential_name = ?),
                           (SELECT COUNT(*) FROM antigravity_warmup_leases
                            WHERE ip_key = ?)""",
                    (filename, ip_key),
                ) as cursor:
                    account_inflight, group_inflight = await cursor.fetchone()
                if account_inflight >= limits["account_concurrency"]:
                    await db.rollback()
                    return {
                        "enabled": True,
                        "acquired": False,
                        "reason": "account_concurrency",
                        "retry_after": 0,
                    }
                if group_inflight >= group_limits["ip_concurrency"]:
                    await db.rollback()
                    return {
                        "enabled": True,
                        "acquired": False,
                        "reason": "ip_concurrency",
                        "retry_after": 0,
                    }
                retry_after = max(account[2] - now, group[1] - now, 0)
                if retry_after > 0:
                    await db.rollback()
                    reason = (
                        "account_interval"
                        if account[2] >= group[1]
                        else "ip_interval"
                    )
                    return {
                        "enabled": True,
                        "acquired": False,
                        "reason": reason,
                        "retry_after": retry_after,
                    }

                lease_id = os.urandom(16).hex()
                await db.execute(
                    """UPDATE antigravity_warmup_accounts
                       SET next_start_at = ?, updated_at = ? WHERE credential_name = ?""",
                    (now + limits["account_interval"], now, filename),
                )
                await db.execute(
                    """UPDATE antigravity_warmup_ip_groups
                       SET next_start_at = ?, updated_at = ? WHERE ip_key = ?""",
                    (now + group_limits["ip_interval"], now, ip_key),
                )
                await db.execute(
                    """INSERT INTO antigravity_warmup_leases (
                           lease_id, credential_name, ip_key, created_at, expires_at
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        lease_id,
                        filename,
                        ip_key,
                        now,
                        now + limits["lease_ttl_seconds"],
                    ),
                )
                await db.commit()
                return {"enabled": True, "acquired": True, "lease_id": lease_id}
            except Exception:
                await db.rollback()
                raise

    async def release_antigravity_warmup_lease(self, lease_id: Optional[str]) -> bool:
        self._ensure_initialized()
        if not lease_id:
            return True
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM antigravity_warmup_leases WHERE lease_id = ?",
                (lease_id,),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def record_antigravity_warmup_result(
        self,
        lease_id: Optional[str],
        outcome: str,
        *,
        now: Optional[float] = None,
    ) -> bool:
        self._ensure_initialized()
        if not lease_id:
            return False
        from src.antigravity_dynamic_warmup import normalize_warmup_config, warmup_limits

        now = time.time() if now is None else float(now)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """SELECT credential_name, ip_key FROM antigravity_warmup_leases
                       WHERE lease_id = ?""",
                    (lease_id,),
                ) as cursor:
                    lease = await cursor.fetchone()
                if not lease:
                    await db.rollback()
                    return False
                filename, ip_key = lease
                await db.execute(
                    "DELETE FROM antigravity_warmup_leases WHERE lease_id = ?",
                    (lease_id,),
                )

                async def advance(table: str, key: str, value: str, config_json: str) -> None:
                    async with db.execute(
                        f"""SELECT level, success_count, window_started_at
                            FROM {table} WHERE {key} = ?""",
                        (value,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    config = normalize_warmup_config(json.loads(config_json or "{}"))
                    limits = warmup_limits(config, row[0])
                    count = int(row[1]) + 1
                    level = int(row[0])
                    window_started = min(float(row[2] or now), now)
                    if (
                        count >= limits["success_threshold"]
                        and now - window_started >= limits["window_seconds"]
                    ):
                        level = min(level + 1, limits["max_level"])
                        count = 0
                        window_started = now
                    await db.execute(
                        f"""UPDATE {table}
                            SET level = ?, success_count = ?, window_started_at = ?, updated_at = ?
                            WHERE {key} = ?""",
                        (level, count, window_started, now, value),
                    )

                async def demote(table: str, key: str, value: str) -> None:
                    await db.execute(
                        f"""UPDATE {table}
                            SET level = MAX(level - 1, 0), success_count = 0,
                                window_started_at = ?, next_start_at = MAX(next_start_at, ?),
                                updated_at = ?
                            WHERE {key} = ?""",
                        (now, now + 60, now, value),
                    )

                if outcome == "success":
                    async with db.execute(
                        """SELECT status, config_json FROM antigravity_warmup_accounts
                           WHERE credential_name = ?""",
                        (filename,),
                    ) as cursor:
                        account = await cursor.fetchone()
                    if account and account[0] == "warming":
                        await advance(
                            "antigravity_warmup_accounts",
                            "credential_name",
                            filename,
                            account[1],
                        )
                        async with db.execute(
                            """SELECT config_json FROM antigravity_warmup_ip_groups
                               WHERE ip_key = ?""",
                            (ip_key,),
                        ) as cursor:
                            group = await cursor.fetchone()
                        if group:
                            await advance(
                                "antigravity_warmup_ip_groups",
                                "ip_key",
                                ip_key,
                                group[0],
                            )
                elif outcome == "rate_limited":
                    await demote(
                        "antigravity_warmup_accounts", "credential_name", filename
                    )
                    await demote("antigravity_warmup_ip_groups", "ip_key", ip_key)
                elif outcome == "paused":
                    await db.execute(
                        """UPDATE antigravity_warmup_accounts
                           SET status = 'paused', updated_at = ? WHERE credential_name = ?""",
                        (now, filename),
                    )
                elif outcome == "forbidden":
                    await db.execute(
                        """UPDATE antigravity_warmup_accounts
                           SET status = 'account_forbidden', updated_at = ?
                           WHERE credential_name = ?""",
                        (now, filename),
                    )
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def list_antigravity_warmup(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_initialized()
        from src.antigravity_dynamic_warmup import (
            normalize_warmup_config,
            warmup_ip_key,
            warmup_limits,
            warmup_selectable,
        )

        accounts: Dict[str, Dict[str, Any]] = {}
        groups: Dict[str, Dict[str, Any]] = {}
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                """SELECT c.filename, COALESCE(w.status, 'off'),
                          COALESCE(w.level, 0), COALESCE(w.success_count, 0),
                          w.window_started_at, COALESCE(w.next_start_at, 0),
                          COALESCE(w.config_json, '{}'), c.proxy_mode,
                          c.bound_proxy_node_id, h.egress_ip, c.proxy_url
                   FROM antigravity_credentials c
                   LEFT JOIN antigravity_warmup_accounts w
                     ON w.credential_name = c.filename
                   LEFT JOIN antigravity_account_health h
                     ON h.credential_name = c.filename
                   ORDER BY c.filename"""
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                config = normalize_warmup_config(json.loads(row[6] or "{}"))
                ip_key = warmup_ip_key(
                    {
                        "proxy_mode": row[7],
                        "bound_proxy_node_id": row[8],
                        "proxy_url": row[10],
                    },
                    {"egress_ip": row[9]},
                )
                accounts[row[0]] = {
                    "credential_name": row[0],
                    "status": row[1],
                    "level": row[2],
                    "success_count": row[3],
                    "window_started_at": row[4],
                    "next_start_at": row[5],
                    "config": config,
                    "limits": warmup_limits(config, row[2]),
                    "selectable": warmup_selectable(row[1]),
                    "ip_key": ip_key,
                }

            async with db.execute(
                """SELECT ip_key, level, success_count, window_started_at,
                          next_start_at, config_json
                   FROM antigravity_warmup_ip_groups ORDER BY ip_key"""
            ) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                config = normalize_warmup_config(json.loads(row[5] or "{}"))
                groups[row[0]] = {
                    "ip_key": row[0],
                    "level": row[1],
                    "success_count": row[2],
                    "window_started_at": row[3],
                    "next_start_at": row[4],
                    "config": config,
                    "limits": warmup_limits(config, row[1]),
                }
        return {"accounts": accounts, "ip_groups": groups}

    async def set_antigravity_warmup_ip_config(
        self, ip_key: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        self._ensure_initialized()
        from src.antigravity_dynamic_warmup import normalize_warmup_config

        if not ip_key or len(ip_key) > 256:
            raise ValueError("无效的 IP 组标识")
        now = time.time()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    """SELECT config_json FROM antigravity_warmup_ip_groups
                       WHERE ip_key = ?""",
                    (ip_key,),
                ) as cursor:
                    row = await cursor.fetchone()
                previous = json.loads(row[0] or "{}") if row else {}
                effective = normalize_warmup_config(config, base=previous)
                await db.execute(
                    """INSERT INTO antigravity_warmup_ip_groups (
                           ip_key, config_json, updated_at
                       ) VALUES (?, ?, ?)
                       ON CONFLICT(ip_key) DO UPDATE SET
                           config_json = excluded.config_json,
                           updated_at = excluded.updated_at""",
                    (ip_key, json.dumps(effective), now),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        groups = (await self.list_antigravity_warmup())["ip_groups"]
        return groups[ip_key]
