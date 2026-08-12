"""Proxy-group and batch account-network APIs for the control panel."""

from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from src.antigravity_full_check import run_account_full_check
from src.proxy_groups import mask_proxy_url, normalize_proxy_url, parse_proxy_import
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


class ProxyGroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    strategy: Literal["round_robin", "random", "failover"] | None = None
    enabled: bool | None = None


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
    _token: str = Depends(verify_panel_token),
):
    """全链路检测：代理测试 → 出口/资格检测 → 消息测试，任一失败即停止。"""
    safe_name = _safe_credential_name(filename)
    try:
        result = await run_account_full_check(safe_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = await _account_network_health_payload(safe_name)
    payload.update(result)
    return payload


@router.post("/creds/network-health/{filename}/rebind")
async def rebind_account_network(
    filename: str,
    _token: str = Depends(verify_panel_token),
):
    """Move a group account to a new node, then validate its IP and region."""
    safe_name = _safe_credential_name(filename)
    storage = await get_storage_adapter()
    credential = await storage.get_credential(safe_name, mode="antigravity")
    if not credential:
        raise HTTPException(status_code=404, detail="Antigravity 凭证不存在")
    try:
        rebound = await credential_manager.rebind_antigravity_account(
            safe_name, credential
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not rebound:
        raise HTTPException(
            status_code=409,
            detail="无法重绑：请确认账号使用代理组、组内存在其他可用节点，并已配置 IP 检测地址",
        )
    return await _account_network_health_payload(safe_name)


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
