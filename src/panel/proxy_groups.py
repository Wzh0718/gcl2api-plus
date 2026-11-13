"""Proxy-group and batch account-network APIs for the control panel."""

from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr

from src.antigravity_full_check import run_account_full_check
from src.proxy_groups import mask_proxy_url, normalize_proxy_url, parse_proxy_import
from src.px6 import PX6Error, fetch_owned_proxies
from src.credential_manager import credential_manager
from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token
from config import (
    get_antigravity_egress_ip_check_url,
    get_antigravity_network_check_enabled,
    get_antigravity_network_check_ttl_seconds,
)


router = APIRouter(tags=["proxy-groups"])


class ProxyGroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    strategy: Literal["round_robin", "random", "failover"] = "round_robin"
    enabled: bool = True
    exclusive_assignment: bool = False


class ProxyGroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    strategy: Literal["round_robin", "random", "failover"] | None = None
    enabled: bool | None = None
    exclusive_assignment: bool | None = None


class ProxyImportRequest(BaseModel):
    content: str | None = Field(default=None, max_length=256 * 1024)
    proxies: list[Any] | dict[str, Any] | None = Field(default=None)
    replace: bool = False


class BatchNetworkRequest(BaseModel):
    filenames: list[str] = Field(min_length=1, max_length=1000)
    proxy_mode: Literal["inherit", "custom", "direct", "group"] = "inherit"
    proxy_url: str | None = None
    proxy_group_id: int | None = None


def _public_group(group: dict[str, Any]) -> dict[str, Any]:
    result = dict(group)
    result["nodes"] = [
        {**node, "proxy_url": mask_proxy_url(node.get("proxy_url"))}
        for node in group.get("nodes", [])
    ]
    return result


def _safe_credential_name(filename: str) -> str:
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.endswith(".json"):
        raise HTTPException(status_code=400, detail="无效的凭证文件名")
    return safe_name


async def _account_network_health_payload(filename: str) -> dict[str, Any]:
    storage = await get_storage_adapter()
    credential = await storage.get_credential(filename, mode="antigravity")
    if not credential:
        raise HTTPException(status_code=404, detail="Antigravity 凭证不存在")
    network = await storage.get_credential_network(filename, mode="antigravity")
    health = await storage.get_antigravity_account_health(filename)
    public_network = dict(network)
    public_network["proxy_url"] = mask_proxy_url(public_network.get("proxy_url"))
    public_network["bound_proxy_url"] = mask_proxy_url(
        public_network.get("bound_proxy_url")
    )
    return {
        "filename": filename,
        "network": public_network,
        "health": health,
        "check_config": {
            "enabled": await get_antigravity_network_check_enabled(),
            "ttl_seconds": await get_antigravity_network_check_ttl_seconds(),
            "ip_check_url_configured": bool(
                await get_antigravity_egress_ip_check_url()
            ),
        },
    }



@router.get("/creds/network-health/{filename}")
async def get_account_network_health(
    filename: str,
    _token: str = Depends(verify_panel_token),
):
    """Show the account -> bound node -> egress IP -> eligibility chain."""
    return await _account_network_health_payload(_safe_credential_name(filename))


@router.post("/creds/network-health/{filename}/check")
async def check_account_network_health(
    filename: str,
    skip_proxy: bool = False,
    reconcile: bool = False,
    _token: str = Depends(verify_panel_token),
):
    """检测账号；批量检测可跳过代理阶段，并按结果恢复或禁用账号。"""
    safe_name = _safe_credential_name(filename)
    try:
        result = await run_account_full_check(safe_name, check_proxy=not skip_proxy)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if reconcile:
        if result.get("ok"):
            await credential_manager.record_api_call_result(
                safe_name,
                True,
                mode="antigravity",
                model_name="gemini-2.5-flash",
            )
        await credential_manager.set_cred_disabled(
            safe_name, not result.get("ok"), mode="antigravity"
        )
    payload = await _account_network_health_payload(safe_name)
    payload.update(result)
    return payload


@router.post("/creds/network-health/{filename}/rebind")
async def rebind_account_network(
    filename: str,
    _token: str = Depends(verify_panel_token),
):
    """Save a manual node replacement; report validation independently."""
    safe_name = _safe_credential_name(filename)
    storage = await get_storage_adapter()
    credential = await storage.get_credential(safe_name, mode="antigravity")
    if not credential:
        raise HTTPException(status_code=404, detail="Antigravity 凭证不存在")
    network = await storage.get_credential_network(safe_name, mode="antigravity")
    if network.get("proxy_mode") != "group":
        raise HTTPException(status_code=400, detail="只有使用代理组的账号可以更换节点")
    rebound = await storage.invalidate_antigravity_proxy_binding(
        safe_name, require_replacement=True
    )
    if not rebound:
        raise HTTPException(
            status_code=409,
            detail="未更换节点：账号尚未绑定节点或代理组内没有其他可用节点，已保留原绑定。请检查备用节点是否启用、冷却、过期或已被独占。",
        )
    check_status = "skipped"
    message = "节点已更换并保存；未配置 IP 检测地址，已跳过出口与地区资格检测。"
    if await get_antigravity_egress_ip_check_url():
        try:
            health = await credential_manager.check_antigravity_account_health(
                safe_name, credential
            )
            passed = (
                health.get("binding_status") == "healthy"
                and health.get("eligibility_status") == "eligible"
            )
            check_status = "passed" if passed else "failed"
            message = "节点已更换并保存，出口检测通过。" if passed else (
                "节点已更换并保存，但出口检测未通过："
                + str(health.get("eligibility_reason") or health.get("eligibility_status") or "未知原因")
            )
        except Exception as exc:
            check_status = "failed"
            message = f"节点已更换并保存，但出口检测失败：{exc}"
    payload = await _account_network_health_payload(safe_name)
    payload.update(rebound=True, check_status=check_status, message=message)
    return payload


@router.get("/proxy-groups")
async def list_proxy_groups(_token: str = Depends(verify_panel_token)):
    storage = await get_storage_adapter()
    return {"items": [_public_group(group) for group in await storage.list_proxy_groups()]}


@router.post("/proxy-groups", status_code=201)
async def create_proxy_group(
    payload: ProxyGroupCreateRequest,
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    try:
        return _public_group(
            await storage.create_proxy_group(
                name=payload.name,
                description=payload.description,
                strategy=payload.strategy,
                enabled=payload.enabled,
                exclusive_assignment=payload.exclusive_assignment,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/proxy-groups/{group_id}")
async def update_proxy_group(
    group_id: int,
    payload: ProxyGroupUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    try:
        return _public_group(
            await storage.update_proxy_group(group_id, **payload.model_dump(exclude_none=True))
        )
    except ValueError as exc:
        status = 409 if "存在" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.delete("/proxy-groups/{group_id}")
async def delete_proxy_group(group_id: int, _token: str = Depends(verify_panel_token)):
    storage = await get_storage_adapter()
    try:
        if not await storage.delete_proxy_group(group_id):
            raise HTTPException(status_code=404, detail="代理组不存在")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"success": True, "group_id": group_id}


@router.post("/proxy-groups/{group_id}/import")
async def import_proxy_group_nodes(
    group_id: int,
    payload: ProxyImportRequest,
    _token: str = Depends(verify_panel_token),
):
    raw: Any = payload.proxies if payload.proxies is not None else payload.content
    if raw is None:
        raise HTTPException(status_code=400, detail="请提供代理文本或 proxies 数组")
    try:
        nodes = parse_proxy_import(raw)
        result = await (await get_storage_adapter()).import_proxy_group_nodes(
            group_id, nodes, replace=payload.replace
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**result, "nodes": [{**node, "proxy_url": mask_proxy_url(node["proxy_url"])} for node in nodes]}


class PX6ImportRequest(BaseModel):
    api_key: SecretStr
    protocol: Literal["http", "socks5"] = "http"
    replace: bool = False


@router.post("/proxy-groups/px6/preview")
async def preview_px6_proxies(
    payload: PX6ImportRequest,
    _token: str = Depends(verify_panel_token),
):
    try:
        result = await fetch_owned_proxies(payload.api_key.get_secret_value(), payload.protocol)
    except PX6Error as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    from fastapi.responses import JSONResponse
    return JSONResponse({"count": len(result["nodes"]), "items": result["items"]},
                        headers={"Cache-Control": "no-store"})


@router.post("/proxy-groups/{group_id}/px6")
async def import_px6_proxies(
    group_id: int,
    payload: PX6ImportRequest,
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    try:
        await storage.get_proxy_group(group_id)
        result = await fetch_owned_proxies(payload.api_key.get_secret_value(), payload.protocol)
        if not result["nodes"]:
            raise PX6Error("PX6 账户下没有有效的 HTTP / SOCKS5 代理，未修改代理组")
        imported = await storage.import_proxy_group_nodes(group_id, result["nodes"], replace=payload.replace)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    from fastapi.responses import JSONResponse
    return JSONResponse({**imported, "count": len(result["nodes"]), "items": result["items"]},
                        headers={"Cache-Control": "no-store"})


@router.post("/creds/network/batch")
async def batch_update_credential_network(
    payload: BatchNetworkRequest,
    _token: str = Depends(verify_panel_token),
):
    if payload.proxy_mode == "custom":
        if not payload.proxy_url:
            raise HTTPException(status_code=400, detail="custom 代理模式必须提供 proxy_url")
        try:
            payload.proxy_url = normalize_proxy_url(payload.proxy_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    storage = await get_storage_adapter()
    try:
        result = await storage.batch_update_credential_network(
            payload.filenames,
            proxy_mode=payload.proxy_mode,
            proxy_url=payload.proxy_url,
            proxy_group_id=payload.proxy_group_id,
            mode="antigravity",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result.get("missing"):
        return {**result, "message": "部分凭证不存在，未执行任何更新"}
    return {**result, "message": f"已批量更新 {result['updated_count']} 个 Antigravity 账号"}
