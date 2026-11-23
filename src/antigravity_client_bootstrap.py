"""Antigravity 官方客户端 bootstrap 序列补全（best-effort）。

真实客户端（实抓 2026-11-23，agy 1.5.9）在一次会话开始时会先打三类端点：

1. ``POST /v1internal:fetchAdminControls``  body ``{"project": "<project>"}``
2. ``GET  https://aicode.googleapis.com/v1alpha:fetchConfig``  空 body
3. ``POST /v1internal:writeTrajectoryAcls``  body ``{"trajectoryId": "<uuid>"}``（每条轨迹一次）

网关此前只调用 loadCodeAssist / fetchAvailableModels / retrieveUserQuotaSummary
等，1 与 3 完全缺失，2 也从未调用。本模块只负责补齐，不参与请求主链路决策：

* 全部失败静默降级，绝不把 bootstrap 失败变成请求失败；
* 账号级端点（1、2）按「每账号每天一次」去重，避免把低频端点打成高频特征；
* 轨迹级端点（3）由调用方按 trajectory_id 去重一次。

开关：默认开启；设 ``ANTIGRAVITY_CLIENT_BOOTSTRAP_ENABLED=0`` 可关闭。
``fetchConfig`` 走 aicode 主机，需要该主机在 native 白名单内
（``ANTIGRAVITY_NATIVE_TRANSPORT_ALLOWED_HOSTS``），否则自动跳过。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from log import log

_CONFIG_KEY = "antigravity_client_bootstrap_enabled"
_SEEN_TTL_SECONDS = 24 * 3600
_AICODE_CONFIG_URL = "https://aicode.googleapis.com/v1alpha:fetchConfig"
_ALLOWED_HOSTS_ENV = "ANTIGRAVITY_NATIVE_TRANSPORT_ALLOWED_HOSTS"

# 进程内去重：Redis 不可用时的退路，单进程语义与缓存后端一致。
_memory_seen: Dict[str, float] = {}


def bootstrap_enabled() -> bool:
    """默认开启；只有显式设置为假值时才关闭。"""
    raw = os.getenv("ANTIGRAVITY_CLIENT_BOOTSTRAP_ENABLED", "").strip().lower()
    if not raw:
        return True
    return raw not in {"0", "false", "no", "off"}


def _memory_claim(key: str, ttl: int) -> bool:
    now = time.time()
    expired = [k for k, exp in _memory_seen.items() if exp <= now]
    for k in expired:
        _memory_seen.pop(k, None)
    if key in _memory_seen:
        return False
    _memory_seen[key] = now + ttl
    return True


async def _claim_once(key: str, ttl: int = _SEEN_TTL_SECONDS) -> bool:
    """抢占「今天已跑过」标记。Redis 可用时多进程共享，否则退回进程内。"""
    try:
        from src.redis_config import create_redis_client_from_env

        client = create_redis_client_from_env()
    except Exception:
        client = None
    if client is None:
        return _memory_claim(key, ttl)
    try:
        claimed = await client.set(key, "1", nx=True, ex=ttl)
        return bool(claimed)
    except Exception as exc:  # noqa: BLE001 - 缓存故障不得影响请求
        log.debug(f"[AGY BOOTSTRAP] redis claim 失败，退回进程内: {exc}")
        return _memory_claim(key, ttl)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


def _aicode_allowed() -> bool:
    hosts = {
        value.strip().lower()
        for value in os.getenv(_ALLOWED_HOSTS_ENV, "").split(",")
        if value.strip()
    }
    return "aicode.googleapis.com" in hosts


async def _post_fetch_admin_controls(
    *, access_token: str, project_id: str, proxy_url: Any, credential_data: Dict[str, Any], credential_name: str
) -> Optional[int]:
    from src.api.antigravity import _post_with_host_fallback, build_antigravity_headers

    headers = build_antigravity_headers(
        access_token, credential_data=credential_data, credential_name=credential_name
    )
    response = await _post_with_host_fallback(
        "/v1internal:fetchAdminControls",
        headers,
        json_body={"project": project_id},
        proxy_url=proxy_url,
        timeout=20.0,
    )
    return getattr(response, "status_code", None)


async def _get_aicode_fetch_config(
    *, access_token: str, proxy_url: Any, credential_data: Dict[str, Any], credential_name: str
) -> Optional[int]:
    if not _aicode_allowed():
        return None
    from src.agy_native_transport import agy_native_transport
    from src.api.antigravity import build_antigravity_headers

    headers = build_antigravity_headers(
        access_token, credential_data=credential_data, credential_name=credential_name
    )
    headers.pop("Content-Type", None)
    response = await agy_native_transport.request(
        "GET", _AICODE_CONFIG_URL, headers=headers, proxy_url=proxy_url, timeout=20.0
    )
    return getattr(response, "status_code", None)


async def bootstrap_account_session(
    *,
    credential_name: str,
    access_token: str,
    project_id: str,
    proxy_url: Any = ...,
    credential_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """账号级 bootstrap：每账号每天一次，失败不抛。"""
    result: Dict[str, Any] = {"skipped": True, "admin_controls": None, "fetch_config": None}
    if not bootstrap_enabled() or not access_token or not project_id:
        return result
    key = f"agy:bootstrap:{credential_name}:{time.strftime('%Y%m%d')}"
    if not await _claim_once(key):
        return result
    result["skipped"] = False
    data = dict(credential_data or {})
    try:
        result["admin_controls"] = await _post_fetch_admin_controls(
            access_token=access_token, project_id=project_id, proxy_url=proxy_url,
            credential_data=data, credential_name=credential_name,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[AGY BOOTSTRAP] fetchAdminControls 失败: {type(exc).__name__}: {exc}")
    try:
        result["fetch_config"] = await _get_aicode_fetch_config(
            access_token=access_token, proxy_url=proxy_url,
            credential_data=data, credential_name=credential_name,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[AGY BOOTSTRAP] fetchConfig 失败: {type(exc).__name__}: {exc}")
    log.info(
        f"[AGY BOOTSTRAP] {credential_name} admin_controls={result['admin_controls']} "
        f"fetch_config={result['fetch_config']}"
    )
    return result


async def write_trajectory_acl(
    *,
    credential_name: str,
    trajectory_id: str,
    access_token: str,
    proxy_url: Any = ...,
    credential_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """轨迹级 bootstrap：每条 trajectory 一次，失败返回 False。"""
    if not bootstrap_enabled() or not access_token or not trajectory_id:
        return False
    key = f"agy:trajacl:{credential_name}:{trajectory_id}"
    if not await _claim_once(key):
        return False
    from src.api.antigravity import _post_with_host_fallback, build_antigravity_headers

    try:
        headers = build_antigravity_headers(
            access_token, credential_data=dict(credential_data or {}), credential_name=credential_name
        )
        response = await _post_with_host_fallback(
            "/v1internal:writeTrajectoryAcls",
            headers,
            json_body={"trajectoryId": trajectory_id},
            proxy_url=proxy_url,
            timeout=20.0,
        )
        status = getattr(response, "status_code", None)
        log.info(f"[AGY BOOTSTRAP] writeTrajectoryAcls {credential_name} status={status}")
        return status == 200
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[AGY BOOTSTRAP] writeTrajectoryAcls 失败: {type(exc).__name__}: {exc}")
        return False
