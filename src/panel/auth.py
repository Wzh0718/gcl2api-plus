"""
认证路由模块 - 处理 /auth/* 相关的HTTP请求
"""

import asyncio
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from config import get_antigravity_egress_ip_check_url
from log import log
from src.antigravity_account_health import parse_egress_identity
from src.auth import (
    add_antigravity_account_by_refresh_token,
    asyncio_complete_auth_flow,
    complete_auth_flow_from_callback_url,
    create_auth_url,
    get_auth_status,
    verify_password,
)
from src.models import (
    LoginRequest,
    AuthStartRequest,
    AuthCallbackRequest,
    AuthCallbackUrlRequest,
    AuthAddByRefreshTokenRequest,
)
from src.egress_probe import observe_egress
from src.httpx_client import get_async
from src.proxy_groups import normalize_proxy_url
from src.storage_adapter import get_storage_adapter
from src.utils import verify_panel_token


# 创建路由器
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(request: LoginRequest):
    """用户登录（简化版：直接返回密码作为token）"""
    try:
        if await verify_password(request.password):
            # 直接使用密码作为token，简化认证流程
            return JSONResponse(content={"token": request.password, "message": "登录成功"})
        else:
            raise HTTPException(status_code=401, detail="密码错误")
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"登录失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/start")
async def start_auth(request: AuthStartRequest, token: str = Depends(verify_panel_token)):
    """开始认证流程，支持自动检测项目ID"""
    try:
        # 如果没有提供项目ID，尝试自动检测
        project_id = request.project_id
        if not project_id:
            log.info("用户未提供项目ID，后续将使用自动检测...")

        # 使用认证令牌作为用户会话标识
        user_session = token if token else None
        result = await create_auth_url(
            project_id, user_session, mode=request.mode
        )

        if result["success"]:
            return JSONResponse(
                content={
                    "auth_url": result["auth_url"],
                    "state": result["state"],
                    "auto_project_detection": result.get("auto_project_detection", False),
                    "detected_project_id": result.get("detected_project_id"),
                }
            )
        else:
            raise HTTPException(status_code=500, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"开始认证流程失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/callback")
async def auth_callback(request: AuthCallbackRequest, token: str = Depends(verify_panel_token)):
    """处理认证回调，支持自动检测项目ID"""
    try:
        # 项目ID现在是可选的，在回调处理中进行自动检测
        project_id = request.project_id

        # 使用认证令牌作为用户会话标识
        user_session = token if token else None
        # 异步等待OAuth回调完成
        result = await asyncio_complete_auth_flow(
            project_id, user_session, mode=request.mode
        )

        if result["success"]:
            # 单项目认证成功
            return JSONResponse(
                content={
                    "credentials": result["credentials"],
                    "file_path": result["file_path"],
                    "message": "认证成功，凭证已保存",
                    "auto_detected_project": result.get("auto_detected_project", False),
                }
            )
        else:
            # 如果需要手动项目ID或项目选择，在响应中标明
            if result.get("requires_manual_project_id"):
                # 使用JSON响应
                return JSONResponse(
                    status_code=400,
                    content={"error": result["error"], "requires_manual_project_id": True},
                )
            elif result.get("requires_project_selection"):
                # 返回项目列表供用户选择
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": result["error"],
                        "requires_project_selection": True,
                        "available_projects": result["available_projects"],
                    },
                )
            else:
                raise HTTPException(status_code=400, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"处理认证回调失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/callback-url")
async def auth_callback_url(request: AuthCallbackUrlRequest, token: str = Depends(verify_panel_token)):
    """从回调URL直接完成认证"""
    try:
        # 验证URL格式
        if not request.callback_url or not request.callback_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="请提供有效的回调URL")

        # 从回调URL完成认证
        result = await complete_auth_flow_from_callback_url(
            request.callback_url, request.project_id, mode=request.mode
        )

        if result["success"]:
            # 单项目认证成功
            return JSONResponse(
                content={
                    "credentials": result["credentials"],
                    "file_path": result["file_path"],
                    "message": "从回调URL认证成功，凭证已保存",
                    "auto_detected_project": result.get("auto_detected_project", False),
                }
            )
        else:
            # 处理各种错误情况
            if result.get("requires_manual_project_id"):
                return JSONResponse(
                    status_code=400,
                    content={"error": result["error"], "requires_manual_project_id": True},
                )
            elif result.get("requires_project_selection"):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": result["error"],
                        "requires_project_selection": True,
                        "available_projects": result["available_projects"],
                    },
                )
            else:
                raise HTTPException(status_code=400, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"从回调URL处理认证失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/add-by-refresh-token")
async def add_by_refresh_token(request: AuthAddByRefreshTokenRequest, token: str = Depends(verify_panel_token)):
    """通过 refresh_token 直接导入 Antigravity 账号（RT 登录，仅支持 antigravity）"""
    try:
        if not request.refresh_token or not request.refresh_token.strip():
            raise HTTPException(status_code=400, detail="refresh_token 不能为空")

        storage = None
        proxy_url = request.proxy_url if request.proxy_url else ...
        if request.proxy_group_id:
            # 从代理组按策略挑选节点作为本次导入的代理，导入成功后账号绑定该组
            from src.storage_adapter import get_storage_adapter

            storage = await get_storage_adapter()
            try:
                group = await storage.get_proxy_group(int(request.proxy_group_id))
            except ValueError:
                raise HTTPException(status_code=400, detail="代理组不存在")
            if not group.get("enabled"):
                raise HTTPException(status_code=400, detail="代理组已停用")
            node = await storage.select_proxy_group_node(int(request.proxy_group_id))
            if not node:
                raise HTTPException(status_code=400, detail="代理组没有可用节点")
            proxy_url = node["proxy_url"]

        result = await add_antigravity_account_by_refresh_token(
            request.refresh_token.strip(),
            proxy_url=proxy_url,
        )

        if result["success"]:
            if storage is not None and request.proxy_group_id:
                try:
                    await storage.update_credential_network(
                        result["file_path"],
                        "group",
                        mode="antigravity",
                        proxy_group_id=int(request.proxy_group_id),
                    )
                except Exception as bind_error:
                    log.warning(f"RT 导入后绑定代理组失败: {bind_error}")
            return JSONResponse(
                content={
                    "credentials": result["credentials"],
                    "file_path": result["file_path"],
                    "email": result.get("email"),
                    "oauth_client_key": result.get("oauth_client_key"),
                    "message": "Refresh Token 导入成功，凭证已保存",
                }
            )
        else:
            raise HTTPException(status_code=400, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"RT 登录失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class RefreshTokenImportRequest(BaseModel):
    refresh_token: SecretStr
    proxy_url: SecretStr | None = None
    proxy_group_id: int | None = Field(default=None, gt=0)


class RefreshImportProxy(BaseModel):
    proxy_mode: Literal["inherit", "custom", "group"] = "inherit"
    proxy_group_id: int | None = Field(default=None, gt=0)
    proxy_url: SecretStr | None = None

    def get_network(self):
        if self.proxy_mode == "group":
            if not self.proxy_group_id:
                raise HTTPException(status_code=400, detail="请选择代理组")
            return self.proxy_group_id, None
        if self.proxy_mode == "custom" and self.proxy_url:
            try:
                return None, normalize_proxy_url(self.proxy_url.get_secret_value())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="自定义代理地址无效") from exc
        return None, None


class RefreshImportBatchRequest(RefreshImportProxy):
    refresh_tokens: list[SecretStr] = Field(min_length=1, max_length=100)


class RefreshImportEditRequest(RefreshImportProxy):
    refresh_token: SecretStr | None = None


# Reasons returned by _reserve_fixed_import_exit. Only the two pool-exhaustion
# reasons disable an item; throttled/failed probes are retried later.
EGRESS_NO_FREE_NODE = "no_free_node"
EGRESS_RESERVED = "reserved"
EGRESS_RATE_LIMITED = "rate_limited"
EGRESS_PROBE_TIMEOUT = "probe_timeout"
EGRESS_PROBE_FAILED = "probe_failed"

# One probe per MIN_INTERVAL (2s) means a fixed 40s budget only covers ~20 nodes;
# size the budget from the pool so a healthy large pool cannot time out.
EGRESS_BUDGET_MIN_SECONDS = 40.0
EGRESS_BUDGET_MAX_SECONDS = 180.0
EGRESS_BUDGET_PER_NODE_SECONDS = 3.0

# A throttled provider is retried shortly; an import must not sit disabled
# because ipapi.io answered one probe with 429.
EGRESS_RETRY_DELAY_SECONDS = 30
EGRESS_RETRY_LIMIT = 10

_EGRESS_RETRY_MESSAGES = {
    EGRESS_RATE_LIMITED: "出口探测被限流，稍后自动重试",
    EGRESS_PROBE_TIMEOUT: "出口探测超时，稍后自动重试",
    EGRESS_PROBE_FAILED: "出口探测失败，稍后自动重试",
}


async def _reserve_fixed_import_exit(storage, group_id: int, check_url: str, lease_id: str):
    """Pick one importable node and lease it for this import.

    Returns ``(selection, reason)`` where selection is ``(proxy_url, egress_ip)``
    or ``None``. The reason is what actually stopped the selection, so callers
    can tell a throttled probe apart from an exhausted pool.
    """
    async def fetch_echo(proxy_url):
        return await get_async(check_url, timeout=8.0, proxy_url=proxy_url, trust_env=False)

    nodes = await storage.list_free_proxy_import_nodes(group_id)
    if not nodes:
        # Distinguish "every exit is bound" from "the free exits are still held
        # for seven days after their account was deleted" — the operator fixes
        # these differently.
        if await storage.count_proxy_import_nodes_held_by_reservations(group_id):
            return None, EGRESS_RESERVED
        return None, EGRESS_NO_FREE_NODE
    budget = min(
        EGRESS_BUDGET_MAX_SECONDS,
        max(EGRESS_BUDGET_MIN_SECONDS, EGRESS_BUDGET_PER_NODE_SECONDS * len(nodes)),
    )

    async def _probe_nodes():
        probed_failure = False
        for node_id, proxy_url in nodes:
            observation = await observe_egress(
                check_url, proxy_url, fetch_echo,
                lambda response: parse_egress_identity(response.json()),
            )
            if observation.status == 429:
                # The provider is throttling every route for a while; the
                # remaining probes would short-circuit on the same cooldown.
                return None, EGRESS_RATE_LIMITED
            if observation.status != 200 or not observation.ip:
                probed_failure = True
                continue
            if await storage.reserve_proxy_import_node(
                group_id, node_id, proxy_url, observation.ip, lease_id,
            ):
                return (proxy_url, observation.ip), ""
            # Another import took the node, or its exit is owned elsewhere.
            probed_failure = True
        return None, EGRESS_PROBE_FAILED if probed_failure else EGRESS_NO_FREE_NODE

    try:
        return await asyncio.wait_for(_probe_nodes(), budget)
    except TimeoutError:
        return None, EGRESS_PROBE_TIMEOUT


@router.post("/import-refresh-token")
async def import_refresh_token(
    request: RefreshTokenImportRequest,
    _token: str = Depends(verify_panel_token),
):
    return await _run_refresh_import(request)


async def _run_refresh_import(request: RefreshTokenImportRequest, claim=None):
    refresh_token = request.refresh_token.get_secret_value().strip()
    if not refresh_token or len(refresh_token) > 8192:
        raise HTTPException(status_code=400, detail="refresh_token 长度无效")
    group_id = request.proxy_group_id
    storage = None
    network = (
        {"queue_item_id": str(claim["id"]), "queue_lease_id": claim["lease_id"]}
        if claim is not None else {}
    )
    proxy_url = ...
    egress_ip = None
    lease_id = None
    try:
        if request.proxy_url and group_id:
            raise HTTPException(status_code=400, detail="请选择一种代理方式")
        if request.proxy_url:
            try:
                proxy_url = normalize_proxy_url(request.proxy_url.get_secret_value())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="自定义代理地址无效") from exc
            network["proxy_url"] = proxy_url
        if group_id:
            check_url = await get_antigravity_egress_ip_check_url()
            if not check_url:
                raise HTTPException(status_code=400, detail="请先配置出口 IP 检测地址")
            storage = await get_storage_adapter()
            lease_id = uuid4().hex
            selection, reason = await _reserve_fixed_import_exit(
                storage, group_id, check_url, lease_id,
            )
            if not selection:
                return {
                    "status": "waiting_for_egress",
                    "reason": reason or EGRESS_NO_FREE_NODE,
                    "message": "等待出口",
                }
            proxy_url, egress_ip = selection
            network["lease_id"] = lease_id

        result = await asyncio.wait_for(
            add_antigravity_account_by_refresh_token(
                refresh_token, proxy_url=proxy_url, import_network=network,
            ),
            timeout=120,
        )
        if not result["success"]:
            return {"status": "failed", "message": "导入失败，请检查凭证、账号资格或重复导入"}
        return {
            "status": "imported",
            "file_path": result["file_path"],
            "email": result.get("email"),
            "egress_ip": egress_ip,
        }
    except HTTPException:
        raise
    except TimeoutError:
        return {"status": "failed", "message": "认证请求超时"}
    except Exception as exc:
        log.error(f"Refresh token import failed: {type(exc).__name__}")
        return {"status": "failed", "message": "导入失败"}
    finally:
        if storage is not None and lease_id:
            try:
                await storage.release_proxy_import_node(lease_id)
            except Exception as exc:
                log.warning(f"Refresh token import lease cleanup failed: {type(exc).__name__}")


@router.post("/refresh-import-batches", status_code=201)
async def create_refresh_import_batch(
    request: RefreshImportBatchRequest, _token: str = Depends(verify_panel_token),
):
    group_id, proxy_url = request.get_network()
    if request.proxy_mode == "custom" and not proxy_url:
        raise HTTPException(status_code=400, detail="请填写自定义代理地址")
    tokens = [token.get_secret_value().strip() for token in request.refresh_tokens]
    queue = (await get_storage_adapter()).refresh_import_queue
    batch_id = await queue.create_batch(
        [token if len(token) <= 8192 else "" for token in tokens], group_id, proxy_url,
    )
    return {"batch_id": batch_id}


@router.get("/refresh-import-batches")
async def list_refresh_import_batches(_token: str = Depends(verify_panel_token)):
    queue = (await get_storage_adapter()).refresh_import_queue
    return {"batches": await queue.list_batches()}


@router.get("/refresh-import-batches/{batch_id}")
async def get_refresh_import_batch(batch_id: str, _token: str = Depends(verify_panel_token)):
    queue = (await get_storage_adapter()).refresh_import_queue
    items = await queue.list_items(batch_id)
    if not items:
        raise HTTPException(status_code=404, detail="批次不存在")
    return {"items": items}


@router.patch("/refresh-import-items/{item_id}")
async def edit_refresh_import_item(
    item_id: int, request: RefreshImportEditRequest, _token: str = Depends(verify_panel_token),
):
    group_id, proxy_url = request.get_network()
    token = request.refresh_token.get_secret_value().strip() if request.refresh_token else None
    if token and len(token) > 8192:
        raise HTTPException(status_code=400, detail="refresh_token 长度无效")
    queue = (await get_storage_adapter()).refresh_import_queue
    try:
        edited = await queue.edit_item(item_id, token, request.proxy_mode, group_id, proxy_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not edited:
        raise HTTPException(status_code=409, detail="该条目状态已变化")
    return {"status": "queued"}


@router.post("/refresh-import-items/{item_id}/enable")
async def enable_refresh_import_item(item_id: int, _token: str = Depends(verify_panel_token)):
    queue = (await get_storage_adapter()).refresh_import_queue
    if not await queue.enable_item(item_id):
        raise HTTPException(status_code=409, detail="该条目状态已变化")
    return {"status": "queued"}


async def _settle_waiting_egress(queue, claim, reason):
    """Turn a failed exit selection into a requeue or a disabled item.

    Only an exhausted pool disables the item. A throttled or timed-out probe
    says nothing about the account, so the item is deferred and retried.
    """
    reason = reason or EGRESS_NO_FREE_NODE
    if reason == EGRESS_RESERVED:
        await queue.disable(claim, "空闲出口 IP 仍在删除账号的 7 天保留期内")
        return
    if reason == EGRESS_NO_FREE_NODE:
        await queue.disable(claim, "没有可用的空闲出口 IP")
        return
    message = _EGRESS_RETRY_MESSAGES.get(reason, "出口探测失败，稍后自动重试")
    await queue.defer(
        claim, message,
        delay_seconds=EGRESS_RETRY_DELAY_SECONDS, max_retries=EGRESS_RETRY_LIMIT,
    )


async def process_next_refresh_import(queue):
    claim = await queue.claim_next()
    if not claim:
        return False
    try:
        payload = queue.read_claim_payload(claim)
        token = payload["refresh_token"]
        existing = await queue.find_existing(token, claim["token_digest"])
        if existing is not None:
            await queue.finish(claim, "imported", existing=existing)
            return True
        request = RefreshTokenImportRequest(
            refresh_token=token, proxy_url=payload["proxy_url"], proxy_group_id=claim["proxy_group_id"],
        )
        result = await _run_refresh_import(request, claim=claim)
        if result["status"] == "waiting_for_egress":
            await _settle_waiting_egress(queue, claim, result.get("reason"))
        elif result["status"] == "failed":
            await queue.finish(claim, "failed", result["message"])
    except asyncio.CancelledError:
        await queue.finish(claim, "queued")
        raise
    except HTTPException as exc:
        await queue.finish(claim, "failed", exc.detail)
    except Exception as exc:
        log.warning(f"Refresh import item failed: {type(exc).__name__}")
        await queue.finish(claim, "failed", "导入失败，可编辑后重试")
    return True


async def refresh_import_worker():
    while True:
        try:
            queue = (await get_storage_adapter()).refresh_import_queue
            if await process_next_refresh_import(queue):
                continue
        except Exception as exc:
            log.warning(f"Refresh import worker failed: {type(exc).__name__}")
        await asyncio.sleep(5)


@router.get("/status/{project_id}")
async def check_auth_status(project_id: str, token: str = Depends(verify_panel_token)):
    """检查认证状态"""
    try:
        if not project_id:
            raise HTTPException(status_code=400, detail="Project ID 不能为空")

        status = get_auth_status(project_id)
        return JSONResponse(content=status)

    except Exception as e:
        log.error(f"检查认证状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
