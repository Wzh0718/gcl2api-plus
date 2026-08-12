"""Antigravity 403 封禁账号定期复检服务。

自动封禁（auto_ban）把因 403 被禁用的账号一直留在禁用状态。本服务按配置间隔
对所有「因 403 被禁用」的账号跑一遍全链路检测（代理 → 出口资格 → 消息），
全部通过则自动解除封禁重新投入使用，仍失败则保持封禁等待下一轮。
"""

from __future__ import annotations

import asyncio
from typing import Any

from log import log
from src.credential_manager import credential_manager


async def recheck_disabled_403_accounts(concurrency: int = 3) -> dict[str, Any]:
    """复检所有因 403 被禁用的 antigravity 账号，通过全链路检测则解除封禁。"""
    from src.antigravity_full_check import run_account_full_check

    statuses = await credential_manager.get_creds_status(mode="antigravity")
    candidates = []
    for filename, state in statuses.items():
        if not state.get("disabled"):
            continue
        health = await credential_manager.get_antigravity_account_health(filename)
        if not health.get("last_403_category"):
            continue
        candidates.append(filename)

    if not candidates:
        log.debug("[403RECHECK] 没有因 403 被封禁的账号，跳过本轮复检")
        return {"checked": 0, "re_enabled": []}

    log.info(f"[403RECHECK] 开始复检 {len(candidates)} 个因 403 被封禁的账号")
    semaphore = asyncio.Semaphore(max(int(concurrency), 1))
    re_enabled: list[str] = []

    async def worker(filename: str) -> None:
        async with semaphore:
            try:
                result = await run_account_full_check(filename)
            except Exception as exc:
                log.warning(
                    f"[403RECHECK] {filename} 复检异常: {type(exc).__name__}: {exc}"
                )
                return
            if result.get("ok"):
                await credential_manager.set_cred_disabled(
                    filename, False, mode="antigravity"
                )
                # 清除 403 记录，避免下一轮重复复检
                await credential_manager.update_antigravity_account_health(
                    filename,
                    last_403_category=None,
                    last_403_reason=None,
                    last_403_at=None,
                )
                re_enabled.append(filename)
                log.info(f"[403RECHECK] {filename} 复检通过，已解除封禁")
            else:
                failed_stage = next(
                    (
                        name
                        for name in ("proxy", "health", "message")
                        if not result["stages"].get(name, {}).get("ok", True)
                        and not result["stages"].get(name, {}).get("skipped")
                    ),
                    "unknown",
                )
                log.info(
                    f"[403RECHECK] {filename} 复检未通过（{failed_stage} 阶段失败），保持封禁"
                )

    await asyncio.gather(*(worker(filename) for filename in candidates))
    log.info(
        f"[403RECHECK] 本轮复检完成：{len(candidates)} 个账号，解禁 {len(re_enabled)} 个"
    )
    return {"checked": len(candidates), "re_enabled": re_enabled}


class Antigravity403RecheckService:
    """按配置间隔运行的 403 封禁账号复检后台服务。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        from config import (
            get_antigravity_403_recheck_enabled,
            get_antigravity_403_recheck_interval,
        )

        if not await get_antigravity_403_recheck_enabled():
            log.info("[403RECHECK] 403 封禁账号定期复检未启用")
            return
        interval = await get_antigravity_403_recheck_interval()
        if interval <= 0:
            log.warning(f"[403RECHECK] 复检间隔无效（{interval}s），服务不启动")
            return
        self._task = asyncio.create_task(
            self._run(interval), name="antigravity_403_recheck"
        )
        log.info(f"[403RECHECK] 403 封禁账号定期复检已启动，间隔 {interval}s")

    async def _run(self, interval: int) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await recheck_disabled_403_accounts()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(f"[403RECHECK] 复检循环异常: {type(exc).__name__}: {exc}")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def restart(self) -> None:
        """配置变更后按最新配置重启服务。"""
        await self.stop()
        await self.start()


recheck_service = Antigravity403RecheckService()
