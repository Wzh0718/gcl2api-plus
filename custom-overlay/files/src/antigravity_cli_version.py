"""AGY CLI 版本动态追踪。

gcli2api 伪装成 Antigravity CLI 访问上游，User-Agent 中的版本号决定上游下发的
模型列表（版本过旧 /models 会缩水）。本模块定期抓取官方 auto-updater manifest，
发现新版本后用真实凭证探测验证（fetchAvailableModels），验证通过才采纳并
持久化，User-Agent 随即热切换。

版本优先级（保底不封顶）：

    生效版本 = semver_max(env 保底 ANTIGRAVITY_CLI_VERSION, 已验证采纳版本)

env 未设置时保底为 DEFAULT_CLI_VERSION（硬编码兜底）；manifest 不可达或探测
持续失败时永远停在保底版本，服务不受影响。env 只托底、不阻止升级。

探测零副作用：不触发 _handle_antigravity_403，不写账号健康状态，探测失败
不会封禁/损伤任何账号。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from log import log

# 硬编码兜底版本：需与当前真实 AGY CLI 版本保持一致（manifest 不可达时使用）。
DEFAULT_CLI_VERSION = "1.3.1"

# 官方 auto-updater manifest 服务（只需其中的 version 字段，不下载二进制）。
DEFAULT_MANIFEST_BASE_URL = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
)

# 已验证采纳版本在 config store 中的 key（程序内部状态，不暴露给用户编辑）。
CONFIG_KEY_ADOPTED_VERSION = "antigravity_cli_version_adopted"

DEFAULT_CHECK_INTERVAL_SECONDS = 12 * 3600
MIN_CHECK_INTERVAL_SECONDS = 600

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def is_valid_version(value: Any) -> bool:
    """版本号必须为严格的 x.y.z——该字符串会进入 HTTP header，必须防注入。"""
    return isinstance(value, str) and bool(_VERSION_RE.match(value))


def version_tuple(value: str) -> Tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def compare_versions(a: str, b: str) -> int:
    """semver 比较：a>b 返回 1，a==b 返回 0，a<b 返回 -1。"""
    ta, tb = version_tuple(a), version_tuple(b)
    return (ta > tb) - (ta < tb)


def get_env_floor() -> str:
    """env 保底版本；未设置或格式非法时回退到硬编码兜底版本。"""
    raw = os.getenv("ANTIGRAVITY_CLI_VERSION", "").strip()
    return raw if is_valid_version(raw) else DEFAULT_CLI_VERSION


def get_manifest_url() -> str:
    """manifest 地址。env 可覆盖为完整 .json URL，否则按 os/arch 拼路径。"""
    base = os.getenv("ANTIGRAVITY_CLI_MANIFEST_URL", "").strip() or DEFAULT_MANIFEST_BASE_URL
    base = base.rstrip("/")
    if base.endswith(".json"):
        return base
    os_type = os.getenv("ANTIGRAVITY_CLI_OS_TYPE", "linux").strip() or "linux"
    arch = os.getenv("ANTIGRAVITY_CLI_ARCH", "amd64").strip() or "amd64"
    return f"{base}/manifests/{os_type}_{arch}.json"


def get_check_interval() -> int:
    raw = os.getenv("ANTIGRAVITY_CLI_VERSION_CHECK_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_CHECK_INTERVAL_SECONDS
    try:
        return max(int(raw), MIN_CHECK_INTERVAL_SECONDS)
    except ValueError:
        return DEFAULT_CHECK_INTERVAL_SECONDS


# ==================== 模块内状态 ====================

_state: Dict[str, Any] = {
    "adopted_version": None,      # 已验证采纳并持久化的版本（None=尚未加载/无）
    "adopted_loaded": False,      # 是否已从 config store 加载过
    "latest_known": None,         # manifest 里看到的最新版本
    "pending_version": None,      # 已发现但尚未通过探测验证的版本
    "last_check_at": None,
    "last_check_ok": None,
    "last_check_error": None,
    "last_probe_at": None,
    "last_probe_result": None,    # verified / failed
    "last_adopted_at": None,
}
_check_lock = asyncio.Lock()


async def load_adopted_version() -> None:
    """从 config store 加载已采纳版本（服务启动时调用一次）。"""
    from config import get_config_value

    try:
        value = await get_config_value(CONFIG_KEY_ADOPTED_VERSION, None)
    except Exception as exc:
        log.warning(f"[CLIVERSION] 加载已采纳版本失败: {type(exc).__name__}: {exc}")
        value = None
    if isinstance(value, str) and is_valid_version(value.strip()):
        _state["adopted_version"] = value.strip()
    else:
        _state["adopted_version"] = None
    _state["adopted_loaded"] = True


def get_effective_version() -> str:
    """当前生效版本 = max(env 保底, 已采纳版本)。同步、无 IO，可供 UA 构建。"""
    floor = get_env_floor()
    adopted = _state.get("adopted_version")
    if adopted and compare_versions(adopted, floor) > 0:
        return adopted
    return floor


def get_status() -> Dict[str, Any]:
    """面板状态快照（同步）。"""
    floor = get_env_floor()
    adopted = _state.get("adopted_version")
    env_explicit = bool(os.getenv("ANTIGRAVITY_CLI_VERSION", "").strip())
    if adopted and compare_versions(adopted, floor) > 0:
        source = "auto-adopted"
    elif env_explicit:
        source = "env-fallback"
    else:
        source = "default-fallback"
    return {
        "current_version": get_effective_version(),
        "source": source,
        "env_floor": floor,
        "env_pinned": env_explicit,
        "adopted_version": adopted,
        "latest_known": _state.get("latest_known"),
        "pending_version": _state.get("pending_version"),
        "last_check_at": _state.get("last_check_at"),
        "last_check_ok": _state.get("last_check_ok"),
        "last_check_error": _state.get("last_check_error"),
        "last_probe_at": _state.get("last_probe_at"),
        "last_probe_result": _state.get("last_probe_result"),
        "last_adopted_at": _state.get("last_adopted_at"),
        "check_interval": get_check_interval(),
        "manifest_url": get_manifest_url(),
    }


# ==================== manifest 抓取 ====================


async def _fetch_manifest() -> Dict[str, Any]:
    from src.httpx_client import get_async

    # proxy_url 使用默认的 ... 哨兵：继承全局代理配置（run.app 可能需要代理）。
    response = await get_async(get_manifest_url(), timeout=15.0)
    if response.status_code != 200:
        raise ValueError(f"manifest HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("manifest 不是 JSON 对象")
    return data


# ==================== 探测验证 ====================


async def _pick_probe_credentials(limit: int = 3) -> List[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """挑选最多 limit 个未禁用且带 token 的 antigravity 凭证及其网络配置。"""
    from src.credential_manager import credential_manager
    from src.storage_adapter import get_storage_adapter

    storage = await get_storage_adapter()
    statuses = await credential_manager.get_creds_status(mode="antigravity")
    picked: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    for filename, state in statuses.items():
        if state.get("disabled"):
            continue
        credential = await storage.get_credential(filename, mode="antigravity")
        if not credential:
            continue
        if not (credential.get("access_token") or credential.get("token")):
            continue
        network = await storage.resolve_credential_network(filename, mode="antigravity")
        picked.append((filename, credential, network))
        if len(picked) >= limit:
            break
    return picked


def _count_models(response: Any) -> Optional[int]:
    try:
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        models = payload.get("models")
        if isinstance(models, list):
            return len(models)
    return None


async def _probe_version(candidate: str) -> Dict[str, Any]:
    """用候选版本 UA 对 fetchAvailableModels 探测。

    判定：任一凭证返回 2xx 即验证通过；401/403 视为凭证问题换下一个；
    其他错误换下一个；全部失败或无凭证则本轮不采纳。
    探测不写任何账号健康状态、不触发 403 处置，零副作用。
    """
    from config import get_antigravity_api_url
    from src.api.antigravity import build_antigravity_headers
    from src.httpx_client import post_async
    from src.proxy_groups import proxy_argument_from_network

    attempts: List[Dict[str, Any]] = []
    try:
        candidates = await _pick_probe_credentials()
    except Exception as exc:
        return {
            "verified": False,
            "reason": f"pick_credentials_error: {type(exc).__name__}: {exc}",
            "attempts": attempts,
        }
    if not candidates:
        return {"verified": False, "reason": "no_credentials", "attempts": attempts}

    api_url = await get_antigravity_api_url()
    for filename, credential, network in candidates:
        token = credential.get("access_token") or credential.get("token")
        try:
            proxy_url = proxy_argument_from_network(network)
        except ValueError as exc:
            attempts.append({"credential": filename, "ok": False, "error": str(exc)})
            continue
        try:
            response = await post_async(
                f"{api_url}/v1internal:fetchAvailableModels",
                json={},
                headers=build_antigravity_headers(token, version_override=candidate),
                timeout=30.0,
                proxy_url=proxy_url,
            )
        except Exception as exc:
            attempts.append(
                {
                    "credential": filename,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if 200 <= response.status_code < 300:
            model_count = _count_models(response)
            attempts.append(
                {
                    "credential": filename,
                    "ok": True,
                    "status_code": response.status_code,
                    "models": model_count,
                }
            )
            return {
                "verified": True,
                "credential": filename,
                "models": model_count,
                "attempts": attempts,
            }
        attempts.append(
            {
                "credential": filename,
                "ok": False,
                "status_code": response.status_code,
            }
        )
    return {"verified": False, "reason": "all_attempts_failed", "attempts": attempts}


# ==================== 采纳 / 重置 ====================


async def _adopt_version(version: str, probe: Dict[str, Any]) -> None:
    from src.storage_adapter import get_storage_adapter

    storage = await get_storage_adapter()
    await storage.set_config(CONFIG_KEY_ADOPTED_VERSION, version)
    _state["adopted_version"] = version
    _state["adopted_loaded"] = True
    _state["pending_version"] = None
    _state["last_adopted_at"] = time.time()
    log.info(
        f"[CLIVERSION] 已采纳新版本 {version}"
        f"（探测凭证 {probe.get('credential')}，模型数 {probe.get('models')}），"
        "User-Agent 热切换生效"
    )


async def reset_adopted_version() -> None:
    """清除已采纳版本，回退到 env 保底（面板「重置为保底版本」按钮）。"""
    from src.storage_adapter import get_storage_adapter

    storage = await get_storage_adapter()
    await storage.set_config(CONFIG_KEY_ADOPTED_VERSION, "")
    _state["adopted_version"] = None
    _state["adopted_loaded"] = True
    log.info(f"[CLIVERSION] 已清除采纳版本，回退到保底版本 {get_env_floor()}")


# ==================== 检查主流程 ====================


async def check_once(trigger: str = "scheduled") -> Dict[str, Any]:
    """执行一轮「抓 manifest → 比较 → 探测 → 采纳」。任何异常都不向上抛。"""
    if _check_lock.locked():
        status = get_status()
        status["busy"] = True
        return status
    async with _check_lock:
        _state["last_check_at"] = time.time()
        try:
            manifest = await _fetch_manifest()
        except Exception as exc:
            _state["last_check_ok"] = False
            _state["last_check_error"] = f"{type(exc).__name__}: {exc}"
            log.warning(f"[CLIVERSION] manifest 抓取失败: {type(exc).__name__}: {exc}")
            return get_status()

        candidate = str(manifest.get("version", "")).strip()
        if not is_valid_version(candidate):
            _state["last_check_ok"] = False
            _state["last_check_error"] = f"manifest 版本号非法: {candidate!r}"
            log.warning(f"[CLIVERSION] manifest 版本号非法，拒绝采纳: {candidate!r}")
            return get_status()

        _state["last_check_ok"] = True
        _state["last_check_error"] = None
        _state["latest_known"] = candidate

        effective = get_effective_version()
        if compare_versions(candidate, effective) <= 0:
            _state["pending_version"] = None
            log.debug(
                f"[CLIVERSION] manifest 版本 {candidate} 不新于当前生效 {effective}（{trigger}）"
            )
            return get_status()

        _state["pending_version"] = candidate
        log.info(
            f"[CLIVERSION] 发现新版本 {candidate}（当前生效 {effective}），开始探测验证"
        )
        _state["last_probe_at"] = time.time()
        probe = await _probe_version(candidate)
        if probe.get("verified"):
            _state["last_probe_result"] = "verified"
            try:
                await _adopt_version(candidate, probe)
            except Exception as exc:
                log.warning(
                    f"[CLIVERSION] 采纳版本 {candidate} 持久化失败: {type(exc).__name__}: {exc}"
                )
        else:
            _state["last_probe_result"] = "failed"
            log.warning(
                f"[CLIVERSION] 新版本 {candidate} 探测未通过"
                f"（{probe.get('reason', 'unknown')}），保持 {effective}"
            )
        return get_status()


# ==================== 后台服务 ====================


class CliVersionUpdateService:
    """按固定间隔运行版本检查的后台服务（常开，无开关配置）。"""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        await load_adopted_version()
        interval = get_check_interval()
        self._task = asyncio.create_task(
            self._run(interval), name="antigravity_cli_version"
        )
        log.info(
            f"[CLIVERSION] AGY CLI 版本追踪已启动，当前生效 {get_effective_version()}，"
            f"检查间隔 {interval}s，manifest {get_manifest_url()}"
        )

    async def _run(self, interval: int) -> None:
        while True:
            try:
                await check_once(trigger="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(f"[CLIVERSION] 版本检查循环异常: {type(exc).__name__}: {exc}")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None


cli_version_service = CliVersionUpdateService()
