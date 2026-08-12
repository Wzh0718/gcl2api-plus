"""Antigravity 账号全链路检测：代理测试 → 出口/资格检测 → 消息测试。

供控制面板手动检测与 403 封禁账号定期复检共用。
任一阶段失败即短路，后续阶段标记 skipped。
"""

from __future__ import annotations

import time
from typing import Any

from src.credential_manager import credential_manager
from src.httpx_client import post_async
from src.proxy_groups import proxy_argument_from_network
from src.storage_adapter import get_storage_adapter


async def _apply_check_403(
    filename: str,
    credential: dict[str, Any],
    network: dict[str, Any],
    error_text: str,
) -> str:
    """检测过程中遇到 403 时，走与运行时一致的分类/封禁处置，返回分类名。"""
    from src.api.antigravity import _handle_antigravity_403

    result = await _handle_antigravity_403(
        credential_name=filename,
        credential_data={**credential, **network},
        model_name="",
        error_text=error_text,
    )
    return result.decision.category


async def _check_proxy_stage(
    filename: str,
    credential: dict[str, Any],
    network: dict[str, Any],
) -> dict[str, Any]:
    """阶段 1：代理测试——通过账号绑定的代理请求上游接口。"""
    from config import get_antigravity_api_url
    from src.api.antigravity import build_antigravity_headers

    try:
        proxy_url = proxy_argument_from_network(network)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    token = credential.get("access_token") or credential.get("token")
    if not token:
        return {"ok": False, "error": "凭证中没有访问令牌"}
    try:
        response = await post_async(
            f"{await get_antigravity_api_url()}/v1internal:fetchAvailableModels",
            json={},
            headers=build_antigravity_headers(token),
            timeout=30.0,
            proxy_url=proxy_url,
        )
    except Exception as exc:
        storage = await get_storage_adapter()
        await storage.update_antigravity_account_health(
            filename,
            binding_status="proxy_failed",
            eligibility_reason=f"代理测试失败: {type(exc).__name__}: {exc}"[:1000],
            eligibility_checked_at=time.time(),
        )
        return {"ok": False, "error": f"代理连接失败: {type(exc).__name__}: {exc}"}
    if response.status_code == 200:
        return {"ok": True, "status_code": 200}
    if response.status_code == 403:
        category = await _apply_check_403(
            filename, credential, network, response.text or ""
        )
        return {
            "ok": False,
            "status_code": 403,
            "category": category,
            "error": (response.text or "")[:500],
        }
    return {
        "ok": False,
        "status_code": response.status_code,
        "error": (response.text or "")[:500],
    }


async def _check_message_stage(
    filename: str,
    credential: dict[str, Any],
    network: dict[str, Any],
) -> dict[str, Any]:
    """阶段 3：消息测试——真实 generateContent 请求验证账号可用。"""
    from config import get_antigravity_api_url
    from src.api.antigravity import build_antigravity_headers

    token = credential.get("access_token") or credential.get("token")
    if not token:
        return {"ok": False, "error": "凭证中没有访问令牌"}
    project_id = credential.get("project_id", "")
    if not project_id:
        return {"ok": False, "error": "凭证中没有项目ID"}
    try:
        proxy_url = proxy_argument_from_network(network)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        response = await post_async(
            f"{await get_antigravity_api_url()}/v1internal:generateContent",
            json={
                "model": "gemini-2.5-flash",
                "project": project_id,
                "request": {
                    "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                    "generationConfig": {"maxOutputTokens": 1},
                },
            },
            headers=build_antigravity_headers(token),
            timeout=30.0,
            proxy_url=proxy_url,
        )
    except Exception as exc:
        return {"ok": False, "error": f"请求失败: {type(exc).__name__}: {exc}"}
    if response.status_code == 200:
        return {"ok": True, "status_code": 200}
    if response.status_code == 403:
        category = await _apply_check_403(
            filename, credential, network, response.text or ""
        )
        return {
            "ok": False,
            "status_code": 403,
            "category": category,
            "error": (response.text or "")[:500],
        }
    if response.status_code == 429:
        return {"ok": True, "status_code": 429, "note": "凭证被限流但有效"}
    return {
        "ok": False,
        "status_code": response.status_code,
        "error": (response.text or "")[:500],
    }


async def run_account_full_check(filename: str) -> dict[str, Any]:
    """按顺序执行全链路检测：代理测试 → 出口/资格检测 → 消息测试。

    Raises:
        ValueError: 凭证不存在。
    """
    storage = await get_storage_adapter()
    credential = await storage.get_credential(filename, mode="antigravity")
    if not credential:
        raise ValueError(f"Antigravity 凭证不存在: {filename}")
    network = await storage.resolve_credential_network(filename, mode="antigravity")

    stages: dict[str, Any] = {}

    proxy_stage = await _check_proxy_stage(filename, credential, network)
    stages["proxy"] = proxy_stage
    if not proxy_stage["ok"]:
        stages["health"] = {"ok": False, "skipped": True}
        stages["message"] = {"ok": False, "skipped": True}
        return {"ok": False, "stages": stages}

    try:
        health = await credential_manager.check_antigravity_account_health(filename)
    except ValueError as exc:
        health = {
            "binding_status": "unknown",
            "eligibility_status": "error",
            "eligibility_reason": str(exc),
        }
    health_ok = (
        health.get("binding_status") == "healthy"
        and health.get("eligibility_status") == "eligible"
    )
    stages["health"] = {"ok": health_ok, **health}
    if not health_ok:
        stages["message"] = {"ok": False, "skipped": True}
        return {"ok": False, "stages": stages}

    # 健康检查可能刷新了 token，重新取一次凭证
    credential = await storage.get_credential(filename, mode="antigravity") or credential
    message_stage = await _check_message_stage(filename, credential, network)
    stages["message"] = message_stage
    return {"ok": bool(message_stage["ok"]), "stages": stages}
