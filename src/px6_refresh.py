"""PX6 代理池：密钥加密落盘 + 定时同步与换绑。

背景（2026-11-20）：

- 面板导入 PX6 时把 key 一次性传给 :func:`src.px6.fetch_owned_proxies`，
  用完即弃（该模块刻意不落盘 key）。结果是代理到期后无法自动续，
  只能人工再填一次 key。
- 节点记录里的 ``expires_at`` 在早期「手工归并」的那批节点上全是 NULL，
  选节点逻辑把 NULL 视为永不过期，于是过期 IP 会一直留在组里。
- 账号对节点是硬绑定（``bound_proxy_node_id``），节点到期后账号不会自己
  挪窝，表现为大面积代理失败。

本模块补上三件事：

1. 用 :class:`src.api_keys.ApiKeyCipher`（本地 Fernet 主密钥）把 PX6 key
   加密后写入 ``config`` 表，只在本机可解密；
2. 定时（默认 1 小时）从 PX6 拉一次已购代理，按 ``proxy_url`` upsert 节点
   并写回 ``expires_at``，顺带停用「已不在授权里且无人绑定」的旧节点；
3. 把绑定在过期/停用节点上的账号迁到同组可用节点，并在账号健康状态上
   标记 ``proxy_rebound`` / ``proxy_failed``，让面板账号列表能直接看到。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from log import log

CONFIG_KEY_CIPHERTEXT = "px6_api_key_ciphertext"
CONFIG_KEY_LAST_SYNC = "px6_last_sync"
GROUP_NAME_HINT = "PX6"
DEFAULT_INTERVAL_SECONDS = 3600


# ----------------------------------------------------------------------
# 密钥存取
# ----------------------------------------------------------------------

def _cipher():
    from src.api_keys import ApiKeyCipher

    return ApiKeyCipher()


async def _storage():
    from src.storage_adapter import get_storage_adapter

    return await get_storage_adapter()


async def save_api_key(raw_key: str) -> bool:
    """加密保存 PX6 API key；空字符串表示清除。"""
    storage = await _storage()
    value = (raw_key or "").strip()
    if not value:
        return await storage.set_config(CONFIG_KEY_CIPHERTEXT, "")
    ciphertext = _cipher().encrypt(value)
    saved = await storage.set_config(CONFIG_KEY_CIPHERTEXT, ciphertext)
    if saved:
        log.info("[PX6] API key 已加密保存")
    return saved


async def load_api_key() -> Optional[str]:
    """读取并解密 PX6 API key；未配置时返回 None。"""
    storage = await _storage()
    ciphertext = await storage.get_config(CONFIG_KEY_CIPHERTEXT)
    if not ciphertext:
        return None
    try:
        return _cipher().decrypt(str(ciphertext))
    except ValueError as exc:
        log.warning(f"[PX6] 保存的 API key 无法解密：{exc}")
        return None


async def key_status() -> Dict[str, Any]:
    """面板展示用：是否已保存、末四位、上次同步结果。"""
    storage = await _storage()
    ciphertext = await storage.get_config(CONFIG_KEY_CIPHERTEXT)
    tail = None
    if ciphertext:
        try:
            raw = _cipher().decrypt(str(ciphertext))
            tail = raw[-4:] if len(raw) >= 4 else "****"
        except ValueError:
            tail = "损坏"
    last = await storage.get_config(CONFIG_KEY_LAST_SYNC) or {}
    return {
        "configured": bool(ciphertext),
        "key_tail": tail,
        "last_sync": last if isinstance(last, dict) else {},
        "interval_seconds": await refresh_interval(),
        "enabled": await refresh_enabled(),
    }


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------

async def refresh_enabled() -> bool:
    from config import get_px6_refresh_enabled

    return await get_px6_refresh_enabled()


async def refresh_interval() -> int:
    from config import get_px6_refresh_interval

    return await get_px6_refresh_interval()


async def resolve_group_id(explicit: Optional[int] = None) -> Optional[int]:
    """定位要维护的代理组：显式 id > 配置 > 名称包含 PX6 的第一个组。"""
    if explicit:
        return int(explicit)
    from config import get_px6_group_id

    configured = await get_px6_group_id()
    if configured:
        return int(configured)
    storage = await _storage()
    groups = await storage.list_proxy_groups()
    for group in groups or []:
        name = str(group.get("name") or "")
        if GROUP_NAME_HINT.lower() in name.lower():
            return int(group["id"])
    return None


# ----------------------------------------------------------------------
# 一次同步
# ----------------------------------------------------------------------

async def sync_once(group_id: Optional[int] = None) -> Dict[str, Any]:
    """拉取 PX6 → upsert 节点 → 停用孤立节点 → 换绑到期账号。

    没有保存 key 时直接跳过（不报错），返回 ``{"skipped": "no_key"}``。
    """
    from src.px6 import PX6Error, fetch_owned_proxies

    api_key = await load_api_key()
    if not api_key:
        return {"skipped": "no_key"}
    resolved = await resolve_group_id(group_id)
    if not resolved:
        return {"skipped": "no_group"}

    storage = await _storage()
    started = time.time()
    try:
        result = await fetch_owned_proxies(api_key)
    except (PX6Error, ValueError) as exc:
        log.warning(f"[PX6] 拉取授权代理失败：{exc}")
        summary = {"error": str(exc), "at": started, "group_id": resolved}
        await storage.set_config(CONFIG_KEY_LAST_SYNC, summary)
        return summary

    nodes = result.get("nodes") or []
    if not nodes:
        summary = {"error": "PX6 账号下没有可用的 HTTP/SOCKS5 代理", "at": started,
                   "group_id": resolved}
        await storage.set_config(CONFIG_KEY_LAST_SYNC, summary)
        return summary

    imported = await storage.import_proxy_group_nodes(resolved, nodes, replace=False)
    disabled = await storage.disable_orphan_proxy_nodes(
        resolved, {node["proxy_url"] for node in nodes}
    )
    rebound = await storage.rebind_expired_proxy_accounts(
        resolved, live_urls={node["proxy_url"] for node in nodes}
    )
    summary = {
        "at": started,
        "group_id": resolved,
        "fetched": len(nodes),
        "imported": imported.get("imported_count", 0),
        "updated": imported.get("updated_count", 0),
        "disabled_orphans": disabled,
        "rebound": len(rebound.get("rebound", [])),
        "unassignable": len(rebound.get("unassignable", [])),
        "rebound_accounts": [item["filename"] for item in rebound.get("rebound", [])][:50],
    }
    await storage.set_config(CONFIG_KEY_LAST_SYNC, summary)
    log.info(
        f"[PX6] 同步完成：拉取 {len(nodes)}、新增 {summary['imported']}、"
        f"更新 {summary['updated']}、停用 {disabled}、换绑 {summary['rebound']}、"
        f"无节点可分配 {summary['unassignable']}"
    )
    return summary


# ----------------------------------------------------------------------
# 定时服务
# ----------------------------------------------------------------------

class PX6RefreshService:
    """按配置间隔同步 PX6 代理池的后台服务。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if not await refresh_enabled():
            log.info("[PX6] 定时同步未启用")
            return
        interval = await refresh_interval()
        if interval <= 0:
            log.warning(f"[PX6] 同步间隔无效（{interval}s），服务不启动")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(interval), name="px6_refresh")
        log.info(f"[PX6] 定时同步已启动，间隔 {interval}s")

    async def _run(self, interval: int) -> None:
        # 启动后先跑一轮，避免刚上线时组里还留着过期节点。
        while True:
            try:
                await sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(f"[PX6] 同步循环异常: {type(exc).__name__}: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()


px6_refresh_service = PX6RefreshService()
