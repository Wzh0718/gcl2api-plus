"""Billing and account-network control-panel APIs."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from config import get_proxy_config
from log import log
from src.api.antigravity import build_antigravity_headers
from src.httpx_client import post_async
from src.proxy_groups import (
    mask_proxy_url as mask_proxy_node_url,
    normalize_proxy_url,
    proxy_argument_from_network,
)
from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token


router = APIRouter(tags=["billing"])

# 价格变更后的历史成本重算任务（按模型去重，运行中不重复调度）
_reprice_tasks: Dict[str, asyncio.Task] = {}


def _schedule_reprice(model: str | None) -> bool:
    """调度一次后台历史成本重算；同模型已有任务在跑时返回 False。"""
    from src.billing import get_billing_recorder
    from src.task_manager import create_managed_task

    key = model or "default"
    existing = _reprice_tasks.get(key)
    if existing is not None and not existing.done():
        return False

    async def runner():
        try:
            storage = await get_storage_adapter()
            recorder = await get_billing_recorder(storage)
            updated = await recorder.recalculate_costs(model)
            log.info(f"[BILLING] 价格变更历史重算完成: model={key}, rows={updated}")
        except Exception as exc:
            log.error(f"[BILLING] 价格变更历史重算失败: model={key}, error={exc}")
        finally:
            _reprice_tasks.pop(key, None)

    _reprice_tasks[key] = create_managed_task(runner(), name=f"billing-reprice-{key}")
    return True


def mask_proxy_url(value: str | None) -> str | None:
    if not value:
        return value
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() == "ss":
            return mask_proxy_node_url(value)
        if not parts.password:
            return value
        user = quote(parts.username or "", safe="")
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, f"{user}:***@{host}", parts.path, parts.query, parts.fragment))
    except Exception:
        return "***"


def _price_payload(model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        input_price = Decimal(str(payload.get("input_price", 0)))
        output_price = Decimal(str(payload.get("output_price", 0)))
        cache_price = Decimal(str(payload.get("cache_price", 0)))
        if not all(value.is_finite() for value in (input_price, output_price, cache_price)):
            raise InvalidOperation("价格必须是有限数字")
        values = {
            "model": model,
            "input_price": f"{input_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}",
            "output_price": f"{output_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}",
            "cache_price": f"{cache_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}",
            "currency": str(payload.get("currency") or os.getenv("BILLING_CURRENCY", "CNY")),
        }
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"价格必须是数字: {exc}") from exc
    if any(Decimal(values[key]) < 0 for key in ("input_price", "output_price", "cache_price")):
        raise HTTPException(status_code=400, detail="价格不能为负数")
    return values


@router.get("/billing/pricing")
async def get_billing_pricing(_token: str = Depends(verify_panel_token)):
    storage = await get_storage_adapter()
    rows = [_price_payload(row["model"], row) for row in await storage.list_billing_prices()]
    default = next((row for row in rows if row["model"] == "default"), None)
    return {"currency": os.getenv("BILLING_CURRENCY", "CNY"), "default": default, "models": [row for row in rows if row["model"] != "default"]}


@router.put("/billing/pricing/default")
async def put_default_pricing(payload: Dict[str, Any] = Body(...), _token: str = Depends(verify_panel_token)):
    storage = await get_storage_adapter()
    result = await storage.upsert_billing_price(**_price_payload("default", payload))
    from src.billing import get_billing_recorder
    await (await get_billing_recorder(storage)).invalidate_price_cache()
    result["recalculating"] = _schedule_reprice(None)
    return result


@router.put("/billing/pricing/models/{model:path}")
async def put_model_pricing(model: str, payload: Dict[str, Any] = Body(...), _token: str = Depends(verify_panel_token)):
    if not model or model == "default":
        raise HTTPException(status_code=400, detail="模型名无效")
    storage = await get_storage_adapter()
    result = await storage.upsert_billing_price(**_price_payload(model, payload))
    from src.billing import get_billing_recorder
    await (await get_billing_recorder(storage)).invalidate_price_cache()
    result["recalculating"] = _schedule_reprice(model)
    return result


@router.delete("/billing/pricing/models/{model:path}")
async def delete_model_pricing(model: str, _token: str = Depends(verify_panel_token)):
    storage = await get_storage_adapter()
    if not await storage.delete_billing_price(model):
        raise HTTPException(status_code=404, detail="模型价格不存在或不可删除")
    from src.billing import get_billing_recorder
    await (await get_billing_recorder(storage)).invalidate_price_cache()
    # 删除独立价格后该模型回落到 default 价格，历史成本同样需要重算
    recalculating = _schedule_reprice(model)
    return {"success": True, "model": model, "recalculating": recalculating}


def _range(value: str) -> str:
    if value not in {"today", "7d", "14d", "30d"}:
        raise HTTPException(status_code=400, detail="range 必须是 today、7d、14d 或 30d")
    return value


@router.get("/billing/summary")
async def billing_summary(
    range: str = "today", api_key_id: str | None = None,
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    return await storage.get_billing_summary(_range(range), api_key_id=api_key_id)


@router.get("/billing/dashboard")
async def billing_dashboard(
    range: str = "today", page_size: int = 10, api_key_id: str | None = None,
    _token: str = Depends(verify_panel_token),
):
    """Prefer Redis for auto-refresh, but keep the SQLite-backed dashboard available."""
    storage = await get_storage_adapter()
    from src.billing import BillingRedisUnavailable, get_billing_recorder
    range_name = _range(range)
    bounded_page_size = min(max(page_size, 1), 100)
    try:
        return await (await get_billing_recorder(storage)).get_redis_dashboard(
            range_name, bounded_page_size, api_key_id=api_key_id
        )
    except BillingRedisUnavailable as exc:
        log.warning(
            f"[BILLING] Redis dashboard unavailable; using SQLite: {type(exc).__name__}"
        )
        summary, accounts, models, keys = await asyncio.gather(
            storage.get_billing_summary(range_name, api_key_id=api_key_id),
            storage.get_billing_accounts(
                range_name, 1, bounded_page_size, api_key_id=api_key_id
            ),
            storage.get_billing_models(
                range_name, 1, bounded_page_size, api_key_id=api_key_id
            ),
            storage.get_billing_keys(range_name, 1, bounded_page_size),
        )
        return {
            "source": "sqlite",
            "degraded": True,
            "scanned_keys": 0,
            "summary": summary,
            "accounts": accounts,
            "models": models,
            "keys": keys,
        }


@router.get("/billing/accounts")
async def billing_accounts(
    range: str = "today", page: int = 1, page_size: int = 50,
    api_key_id: str | None = None, _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    return await storage.get_billing_accounts(
        _range(range), page, min(page_size, 100), api_key_id=api_key_id
    )


@router.get("/billing/models")
async def billing_models(
    range: str = "today", page: int = 1, page_size: int = 50,
    api_key_id: str | None = None, _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    return await storage.get_billing_models(
        _range(range), page, min(page_size, 100), api_key_id=api_key_id
    )


@router.get("/billing/keys")
async def billing_keys(
    range: str = "today", page: int = 1, page_size: int = 50,
    _token: str = Depends(verify_panel_token),
):
    storage = await get_storage_adapter()
    return await storage.get_billing_keys(_range(range), page, min(page_size, 100))


network_router = APIRouter(prefix="/creds", tags=["credentials-network"])


async def _network(filename: str, storage: Any) -> Dict[str, Any]:
    if not filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="无效的文件名")
    data = await storage.get_credential_network(filename, mode="antigravity")
    if await storage.get_credential(filename, mode="antigravity") is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    proxy_mode = data.get("proxy_mode", "inherit")
    if proxy_mode == "custom":
        effective = data.get("proxy_url")
    elif proxy_mode == "direct":
        effective = None
    elif proxy_mode == "inherit":
        effective = await get_proxy_config()
    else:
        effective = None
    group = None
    if proxy_mode == "group" and data.get("proxy_group_id"):
        try:
            group = await storage.get_proxy_group(int(data["proxy_group_id"]))
        except ValueError:
            group = None
    return {
        "filename": filename,
        "proxy_mode": proxy_mode,
        "proxy_url": mask_proxy_url(data.get("proxy_url")),
        "proxy_group_id": data.get("proxy_group_id"),
        "proxy_group": {
            "id": group["id"],
            "name": group["name"],
            "strategy": group["strategy"],
            "enabled_node_count": group["enabled_node_count"],
        }
        if group
        else None,
        "effective_proxy": mask_proxy_url(effective),
    }


@network_router.get("/{filename}/network")
async def get_credential_network(filename: str, mode: str = "antigravity", _token: str = Depends(verify_panel_token)):
    if mode != "antigravity":
        raise HTTPException(status_code=400, detail="账号代理只支持 antigravity")
    return await _network(filename, await get_storage_adapter())


@network_router.patch("/{filename}/network")
async def update_credential_network(filename: str, payload: Dict[str, Any] = Body(...), mode: str = "antigravity", _token: str = Depends(verify_panel_token)):
    if mode != "antigravity":
        raise HTTPException(status_code=400, detail="账号代理只支持 antigravity")
    storage = await get_storage_adapter()
    try:
        proxy_mode = payload.get("proxy_mode", "inherit")
        proxy_url = payload.get("proxy_url")
        if proxy_mode == "custom":
            proxy_url = normalize_proxy_url(proxy_url)
        updated = await storage.update_credential_network(
            filename,
            proxy_mode,
            proxy_url,
            mode="antigravity",
            proxy_group_id=payload.get("proxy_group_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="凭证不存在")
    return await _network(filename, storage)


@network_router.post("/{filename}/network/test")
async def test_credential_network(filename: str, mode: str = "antigravity", _token: str = Depends(verify_panel_token)):
    if mode != "antigravity":
        raise HTTPException(status_code=400, detail="账号代理只支持 antigravity")
    storage = await get_storage_adapter()
    credential = await storage.get_credential(filename, mode="antigravity")
    if not credential:
        raise HTTPException(status_code=404, detail="凭证不存在")
    network = await storage.resolve_credential_network(filename, mode="antigravity")
    credential.update(network)
    try:
        effective = proxy_argument_from_network(network)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    token = credential.get("access_token") or credential.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="凭证中没有访问令牌")
    try:
        from config import get_antigravity_api_url
        response = await post_async(
            f"{await get_antigravity_api_url()}/v1internal:fetchAvailableModels",
            json={}, headers=build_antigravity_headers(token), timeout=30.0, proxy_url=effective,
        )
        return JSONResponse(content={"success": response.status_code == 200, "status_code": response.status_code, "proxy_mode": network.get("proxy_mode", "inherit"), "proxy_group_id": network.get("proxy_group_id"), "proxy_node_id": network.get("proxy_node_id"), "proxy_url": mask_proxy_url(network.get("proxy_url"))}, status_code=200 if response.status_code == 200 else 502)
    except Exception as exc:
        log.warning(f"credential network test failed: {type(exc).__name__}")
        return JSONResponse(content={"success": False, "error": type(exc).__name__, "proxy_mode": network.get("proxy_mode", "inherit")}, status_code=502)
