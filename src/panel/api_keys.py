"""Control-panel APIs for downstream key distribution and quotas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.api_keys import ApiKeyService
from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token


router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    quota_amount: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=8)
    reset_mode: Literal["manual", "monthly"] = "manual"
    expires_at: datetime | None = None


class ApiKeyUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    status: Literal["active", "disabled"] | None = None
    quota_amount: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=8)
    reset_mode: Literal["manual", "monthly"] | None = None
    expires_at: datetime | None = None


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="密钥不存在")
    message = str(exc)
    if (
        "只读" in message
        or "吊销" in message
        or "已存在" in message
        or "重新创建" in message
    ):
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)


@router.get("")
async def list_api_keys(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    status: Literal["active", "disabled", "revoked"] | None = None,
    _token: str = Depends(verify_panel_token),
):
    return await ApiKeyService(await get_storage_adapter()).list_keys(
        page=page, page_size=page_size, status=status
    )


@router.get("/{api_key_id}")
async def get_api_key(api_key_id: str, _token: str = Depends(verify_panel_token)):
    try:
        return await ApiKeyService(await get_storage_adapter()).get_key(api_key_id)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.get("/{api_key_id}/secret")
async def get_api_key_secret(
    api_key_id: str, _token: str = Depends(verify_panel_token)
):
    try:
        api_key = await ApiKeyService(await get_storage_adapter()).get_secret(
            api_key_id
        )
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc
    return JSONResponse(
        {"api_key": api_key}, headers={"Cache-Control": "no-store"}
    )


@router.post("", status_code=201)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    _token: str = Depends(verify_panel_token),
):
    try:
        result = await ApiKeyService(await get_storage_adapter()).create_key(
            name=payload.name,
            quota_amount=payload.quota_amount,
            reset_mode=payload.reset_mode,
            expires_at=payload.expires_at,
        )
    except ValueError as exc:
        raise _error(exc) from exc
    return JSONResponse(
        result,
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


@router.patch("/{api_key_id}")
async def update_api_key(
    api_key_id: str,
    payload: ApiKeyUpdateRequest,
    _token: str = Depends(verify_panel_token),
):
    updates = payload.model_dump(exclude_unset=True)
    try:
        return await ApiKeyService(await get_storage_adapter()).update_key(
            api_key_id, **updates
        )
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc


@router.delete("/{api_key_id}")
async def revoke_api_key(api_key_id: str, _token: str = Depends(verify_panel_token)):
    try:
        found = await ApiKeyService(await get_storage_adapter()).revoke_key(api_key_id)
    except ValueError as exc:
        raise _error(exc) from exc
    if not found:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return {"success": True, "api_key_id": api_key_id}


@router.post("/{api_key_id}/quota-resets")
async def reset_api_key_quota(
    api_key_id: str,
    _token: str = Depends(verify_panel_token),
):
    try:
        return await ApiKeyService(await get_storage_adapter()).reset_quota(api_key_id)
    except (KeyError, ValueError) as exc:
        raise _error(exc) from exc
