"""
AGY CLI 版本动态追踪状态路由 - 处理 /cli-version/* 相关的HTTP请求

只读状态展示 + 手动触发检查 + 重置已采纳版本（回退到 env 保底）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from log import log
from src.antigravity_cli_version import (
    check_once,
    get_status,
    reset_adopted_version,
)
from src.utils import verify_panel_token


# 创建路由器
router = APIRouter(prefix="/cli-version", tags=["cli-version"])


@router.get("/status")
async def cli_version_status(token: str = Depends(verify_panel_token)):
    """获取当前 CLI 版本追踪状态（当前版本/来源/最近检查与探测结果）。"""
    return JSONResponse({"success": True, **get_status()})


@router.post("/check")
async def cli_version_check(token: str = Depends(verify_panel_token)):
    """手动触发一轮 manifest 检查（发现新版本会先探测验证再采纳）。"""
    try:
        status = await check_once(trigger="manual")
        return JSONResponse({"success": True, **status})
    except Exception as e:
        log.warning(f"[CLIVERSION] 手动检查异常: {type(e).__name__}: {e}")
        return JSONResponse({"success": False, "error": f"{type(e).__name__}: {e}"})


@router.post("/reset")
async def cli_version_reset(token: str = Depends(verify_panel_token)):
    """清除已采纳的动态版本，回退到 env 保底版本。"""
    try:
        await reset_adopted_version()
        return JSONResponse({"success": True, **get_status()})
    except Exception as e:
        log.warning(f"[CLIVERSION] 重置采纳版本异常: {type(e).__name__}: {e}")
        return JSONResponse({"success": False, "error": f"{type(e).__name__}: {e}"})
