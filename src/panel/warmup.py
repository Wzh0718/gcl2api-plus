"""Antigravity dynamic warmup control-panel APIs."""

from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from src.antigravity_dynamic_warmup import (
    DEFAULT_WARMUP_CONFIG,
    normalize_warmup_config,
)
from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token
from fastapi import Depends

router = APIRouter(tags=["Antigravity Dynamic Warmup"])


class WarmupBatchRequest(BaseModel):
    filenames: list[str] = Field(min_length=1, max_length=1000)
    action: Literal["start", "cancel", "resume", "complete", "restart"]
    config: dict[str, int] | None = None


class WarmupAccountRequest(BaseModel):
    action: Literal["start", "cancel", "resume", "complete", "restart"]
    config: dict[str, int] | None = None


def _validated_config(config: dict[str, Any]) -> dict[str, int]:
    unknown = set(config) - set(DEFAULT_WARMUP_CONFIG)
    if unknown:
        raise ValueError(f"未知养号参数: {', '.join(sorted(unknown))}")
    return config


def _safe_credential_name(filename: str) -> str:
    safe_name = os.path.basename(filename)
    if safe_name != filename or not safe_name.endswith(".json"):
        raise HTTPException(status_code=400, detail="无效的凭证文件名")
    return safe_name


@router.get("/antigravity/warmup")
async def get_warmup_overview(_token: str = Depends(verify_panel_token)):
    storage = await get_storage_adapter()
    stored = await storage.get_config("antigravity_warmup_defaults", {})
    defaults = normalize_warmup_config(stored)
    return {"defaults": defaults, **await storage._backend.list_antigravity_warmup()}


@router.put("/antigravity/warmup/defaults")
async def save_warmup_defaults(
    config: dict[str, int] = Body(...),
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    try:
        current = await storage.get_config("antigravity_warmup_defaults", {})
        defaults = normalize_warmup_config(_validated_config(config), base=current)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not await storage.set_config("antigravity_warmup_defaults", defaults):
        raise HTTPException(status_code=500, detail="保存养号默认参数失败")
    return {"defaults": defaults}


@router.post("/antigravity/warmup/batch")
async def batch_warmup_action(
    payload: WarmupBatchRequest,
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    try:
        config = _validated_config(payload.config or {})
        results = await storage._backend.set_antigravity_warmup(
            [_safe_credential_name(name) for name in payload.filenames],
            payload.action,
            config or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"results": results}


@router.put("/antigravity/warmup/ip-groups/{ip_key}")
async def save_warmup_ip_group(
    ip_key: str,
    config: dict[str, int] = Body(...),
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    try:
        return await storage._backend.set_antigravity_warmup_ip_config(
            ip_key, _validated_config(config)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/antigravity/warmup/{filename}")
async def account_warmup_action(
    filename: str,
    payload: WarmupAccountRequest,
    _token: str = Depends(verify_panel_token),
):
    safe_name = _safe_credential_name(filename)
    storage = await get_storage_adapter()
    try:
        config = _validated_config(payload.config or {})
        result = (
            await storage._backend.set_antigravity_warmup(
                [safe_name], payload.action, config or None
            )
        )[safe_name]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result["status"] == "missing":
        raise HTTPException(status_code=404, detail="Antigravity 凭证不存在")
    return result
