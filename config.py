"""
Configuration constants for the Geminicli2api proxy server.
Centralizes all configuration to avoid duplication across modules.

- 启动时加载一次配置到内存
- 修改配置时调用 reload_config() 重新从数据库加载
"""

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv


def load_project_dotenv(dotenv_path: str | Path | None = None) -> bool:
    """Load local runtime settings without overriding injected environment variables."""
    path = (
        Path(dotenv_path)
        if dotenv_path is not None
        else Path(__file__).resolve().with_name(".env")
    )
    return load_dotenv(dotenv_path=path, override=False)


load_project_dotenv()

# 全局配置缓存
_config_cache: dict[str, Any] = {}
_config_initialized = False

# Client Configuration

# 需要自动封禁的错误码 (默认值，可通过环境变量或配置覆盖)
AUTO_BAN_ERROR_CODES = [403]

# ====================== 环境变量映射表 ======================
# 统一维护环境变量名和配置键名的映射关系
# 格式: "环境变量名": "配置键名"
ENV_MAPPINGS = {
    "CODE_ASSIST_ENDPOINT": "code_assist_endpoint",
    "CREDENTIALS_DIR": "credentials_dir",
    "PROXY": "proxy",
    "OAUTH_PROXY_URL": "oauth_proxy_url",
    "GOOGLEAPIS_PROXY_URL": "googleapis_proxy_url",
    "RESOURCE_MANAGER_API_URL": "resource_manager_api_url",
    "SERVICE_USAGE_API_URL": "service_usage_api_url",
    "ANTIGRAVITY_API_URL": "antigravity_api_url",
    "ANTIGRAVITY_NETWORK_CHECK_ENABLED": "antigravity_network_check_enabled",
    "ANTIGRAVITY_NETWORK_CHECK_TTL_SECONDS": "antigravity_network_check_ttl_seconds",
    "ANTIGRAVITY_EGRESS_IP_CHECK_URL": "antigravity_egress_ip_check_url",
    "ANTIGRAVITY_403_RECHECK_ENABLED": "antigravity_403_recheck_enabled",
    "ANTIGRAVITY_403_RECHECK_INTERVAL": "antigravity_403_recheck_interval",
    "AUTO_BAN": "auto_ban_enabled",
    "AUTO_BAN_ERROR_CODES": "auto_ban_error_codes",
    "RETRY_429_MAX_RETRIES": "retry_429_max_retries",
    "RETRY_429_ENABLED": "retry_429_enabled",
    "RETRY_429_INTERVAL": "retry_429_interval",
    "ANTI_TRUNCATION_MAX_ATTEMPTS": "anti_truncation_max_attempts",
    "SENSITIVE_WORD_REPLACE_ENABLED": "sensitive_word_replace_enabled",
    "SENSITIVE_WORD_REPLACE_RULES": "sensitive_word_replace_rules",
    "COMPATIBILITY_MODE": "compatibility_mode_enabled",
    "RETURN_THOUGHTS_TO_FRONTEND": "return_thoughts_to_frontend",
    "ANTIGRAVITY_STREAM2NOSTREAM": "antigravity_stream2nostream",
    "ANTIGRAVITY_SWITCH_CREDENTIAL": "antigravity_switch_credential_enabled",
    "ANTIGRAVITY_MODEL_FALLBACK_CHAIN": "antigravity_model_fallback_chain",
    "ENABLE_GEMINICLI": "enable_geminicli",
    "BILLING_CURRENCY": "billing_currency",
    "BILLING_DEDUPE_TTL_DAYS": "billing_dedupe_ttl_days",
    "BILLING_REDIS_PREFIX": "billing_redis_prefix",
    "BILLING_REDIS_TTL_DAYS": "billing_redis_ttl_days",
    "BILLING_REDIS_SCAN_BATCH_SIZE": "billing_redis_scan_batch_size",
    "BILLING_REDIS_SCAN_MAX_KEYS": "billing_redis_scan_max_keys",
    "MAX_NON_STREAM_BUFFER_BYTES": "max_non_stream_buffer_bytes",
    "CREDENTIAL_CANDIDATE_LIMIT": "credential_candidate_limit",
    "HOST": "host",
    "PORT": "port",
    "API_PASSWORD": "api_password",
    "PANEL_PASSWORD": "panel_password",
    "PASSWORD": "password",
    "KEEPALIVE_URL": "keepalive_url",
    "KEEPALIVE_INTERVAL": "keepalive_interval",
    "CALL_RECORDS_RETENTION_DAYS": "call_records_retention_days",
}


# ====================== 配置系统 ======================

async def init_config():
    """初始化配置缓存（启动时调用一次）"""
    global _config_cache, _config_initialized

    if _config_initialized:
        return

    try:
        from src.storage_adapter import get_storage_adapter
        storage_adapter = await get_storage_adapter()
        _config_cache = await storage_adapter.get_all_config()
        _config_initialized = True
    except Exception:
        # 初始化失败时使用空缓存
        _config_cache = {}
        _config_initialized = True


async def reload_config():
    """重新加载配置（修改配置后调用）"""
    global _config_cache, _config_initialized

    try:
        from src.storage_adapter import get_storage_adapter
        storage_adapter = await get_storage_adapter()

        # 如果后端支持 reload_config_cache，调用它
        if hasattr(storage_adapter._backend, 'reload_config_cache'):
            await storage_adapter._backend.reload_config_cache()

        # 重新加载配置缓存
        _config_cache = await storage_adapter.get_all_config()
        _config_initialized = True
    except Exception:
        pass


def _get_cached_config(key: str, default: Any = None) -> Any:
    """从内存缓存获取配置（同步）"""
    return _config_cache.get(key, default)


async def get_config_value(key: str, default: Any = None, env_var: Optional[str] = None) -> Any:
    """Get configuration value with priority: ENV > Storage > default."""
    # 确保配置已初始化
    if not _config_initialized:
        await init_config()

    # Priority 1: Environment variable
    if env_var and os.getenv(env_var):
        return os.getenv(env_var)

    # Priority 2: Memory cache
    value = _get_cached_config(key)
    if value is not None:
        return value

    return default


# Configuration getters - all async
async def get_proxy_config():
    """Get proxy configuration."""
    proxy_url = await get_config_value("proxy", env_var="PROXY")
    return proxy_url if proxy_url else None


async def get_auto_ban_enabled() -> bool:
    """Get auto ban enabled setting."""
    env_value = os.getenv("AUTO_BAN")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("auto_ban_enabled", False))


async def get_auto_ban_error_codes() -> list:
    """
    Get auto ban error codes.

    Environment variable: AUTO_BAN_ERROR_CODES (comma-separated, e.g., "400,403")
    Database config key: auto_ban_error_codes
    Default: [400, 403]
    """
    env_value = os.getenv("AUTO_BAN_ERROR_CODES")
    if env_value:
        try:
            return [int(code.strip()) for code in env_value.split(",") if code.strip()]
        except ValueError:
            pass

    codes = await get_config_value("auto_ban_error_codes")
    if codes and isinstance(codes, list):
        return codes
    return AUTO_BAN_ERROR_CODES


async def get_retry_429_max_retries() -> int:
    """Get max retries for 429 errors."""
    env_value = os.getenv("RETRY_429_MAX_RETRIES")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("retry_429_max_retries", 5))


async def get_retry_429_enabled() -> bool:
    """Get 429 retry enabled setting."""
    env_value = os.getenv("RETRY_429_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("retry_429_enabled", True))


async def get_retry_429_interval() -> float:
    """Get 429 retry interval in seconds."""
    env_value = os.getenv("RETRY_429_INTERVAL")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass

    return float(await get_config_value("retry_429_interval", 1))


async def get_anti_truncation_max_attempts() -> int:
    """
    Get maximum attempts for anti-truncation continuation.

    Environment variable: ANTI_TRUNCATION_MAX_ATTEMPTS
    Database config key: anti_truncation_max_attempts
    Default: 3
    """
    env_value = os.getenv("ANTI_TRUNCATION_MAX_ATTEMPTS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("anti_truncation_max_attempts", 3))


async def get_sensitive_word_replace_enabled() -> bool:
    """
    Get sensitive word replacement enabled setting.

    启用后，上行请求的 system prompt 会按规则替换敏感词（如客户端指纹字样），
    避免上游特征匹配触发伪 429。

    Environment variable: SENSITIVE_WORD_REPLACE_ENABLED
    Database config key: sensitive_word_replace_enabled
    Default: False
    """
    env_value = os.getenv("SENSITIVE_WORD_REPLACE_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("sensitive_word_replace_enabled", False))


async def get_sensitive_word_replace_rules() -> str:
    """
    Get sensitive word replacement rules text.

    每行一条规则，格式为 "查找词=>替换词"；无 "=>" 时替换为空（即删除）；
    以 "#" 开头的行为注释；匹配区分大小写。

    Environment variable: SENSITIVE_WORD_REPLACE_RULES
    Database config key: sensitive_word_replace_rules
    Default: "" (空，不替换)
    """
    env_value = os.getenv("SENSITIVE_WORD_REPLACE_RULES")
    if env_value:
        return env_value

    rules = await get_config_value("sensitive_word_replace_rules", "")
    return rules if isinstance(rules, str) else ""


async def get_stream_idle_timeout() -> float:
    """
    Get upstream stream idle timeout in seconds.

    流式请求上游超过该时长无任何数据时主动断开，释放连接，避免僵尸流泄漏 fd。

    Environment variable: STREAM_IDLE_TIMEOUT
    Database config key: stream_idle_timeout
    Default: 300 (0 表示禁用)
    """
    env_value = os.getenv("STREAM_IDLE_TIMEOUT")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass

    return float(await get_config_value("stream_idle_timeout", 300))


# Server Configuration
async def get_server_host() -> str:
    """
    Get server host setting.

    Environment variable: HOST
    Database config key: host
    Default: 0.0.0.0
    """
    return str(await get_config_value("host", "0.0.0.0", "HOST"))


async def get_server_port() -> int:
    """
    Get server port setting.

    Environment variable: PORT
    Database config key: port
    Default: 7861
    """
    env_value = os.getenv("PORT")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("port", 7861))


async def get_api_password() -> str:
    """
    Get API password setting for chat endpoints.

    Environment variable: API_PASSWORD
    Database config key: api_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 API_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    api_password = await get_config_value("api_password", None, "API_PASSWORD")
    if api_password is not None:
        return str(api_password)

    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_panel_password() -> str:
    """
    Get panel password setting for web interface.

    Environment variable: PANEL_PASSWORD
    Database config key: panel_password
    Default: Uses PASSWORD env var for compatibility, otherwise 'pwd'
    """
    # 优先使用 PANEL_PASSWORD，如果没有则使用通用 PASSWORD 保证兼容性
    panel_password = await get_config_value("panel_password", None, "PANEL_PASSWORD")
    if panel_password is not None:
        return str(panel_password)

    # 兼容性：使用通用密码
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_server_password() -> str:
    """
    Get server password setting (deprecated, use get_api_password or get_panel_password).

    Environment variable: PASSWORD
    Database config key: password
    Default: pwd
    """
    return str(await get_config_value("password", "pwd", "PASSWORD"))


async def get_credentials_dir() -> str:
    """
    Get credentials directory setting.

    Environment variable: CREDENTIALS_DIR
    Database config key: credentials_dir
    Default: ./creds
    """
    return str(await get_config_value("credentials_dir", "./creds", "CREDENTIALS_DIR"))


async def get_code_assist_endpoint() -> str:
    """
    Get Code Assist endpoint setting.

    Environment variable: CODE_ASSIST_ENDPOINT
    Database config key: code_assist_endpoint
    Default: https://cloudcode-pa.googleapis.com
    """
    return str(
        await get_config_value(
            "code_assist_endpoint", "https://cloudcode-pa.googleapis.com", "CODE_ASSIST_ENDPOINT"
        )
    )


async def get_compatibility_mode_enabled() -> bool:
    """
    Get compatibility mode setting.

    兼容性模式：启用后所有system消息全部转换成user，停用system_instructions。
    该选项可能会降低模型理解能力，但是能避免流式空回的情况。

    Environment variable: COMPATIBILITY_MODE
    Database config key: compatibility_mode_enabled
    Default: False
    """
    env_value = os.getenv("COMPATIBILITY_MODE")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("compatibility_mode_enabled", False))


async def get_return_thoughts_to_frontend() -> bool:
    """
    Get return thoughts to frontend setting.

    控制是否将思维链返回到前端。
    启用后，思维链会在响应中返回；禁用后，思维链会在响应中被过滤掉。

    Environment variable: RETURN_THOUGHTS_TO_FRONTEND
    Database config key: return_thoughts_to_frontend
    Default: True
    """
    env_value = os.getenv("RETURN_THOUGHTS_TO_FRONTEND")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("return_thoughts_to_frontend", True))


async def get_antigravity_stream2nostream() -> bool:
    """
    Get use stream for non-stream setting.

    控制antigravity非流式请求是否使用流式API并收集为完整响应。
    启用后，非流式请求将在后端使用流式API，然后收集所有块后再返回完整响应。

    Environment variable: ANTIGRAVITY_STREAM2NOSTREAM
    Database config key: antigravity_stream2nostream
    Default: False
    """
    env_value = os.getenv("ANTIGRAVITY_STREAM2NOSTREAM")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("antigravity_stream2nostream", False))


# Antigravity hub 轨迹指纹（2026-10-21 起，见 docs/AGY-2.25.0-HOOK-封控对比-2026-10-21.md）
# 这两个键刻意不走 ENV_MAPPINGS：优先级为 面板配置 > ENV > 默认值，
# ENV 只作部署期兜底初始值，面板随时可改（hub 通道周更，需频繁维护）。
_ANTIGRAVITY_HUB_VERSION_DEFAULT = "2.25.0"
_ANTIGRAVITY_HUB_CL_DEFAULT = "1003475615"


async def get_antigravity_hub_version() -> str:
    """hub UA 版本。面板配置 > ENV > 默认。"""
    value = await get_config_value("antigravity_hub_version")
    if value:
        return str(value)
    return os.getenv("ANTIGRAVITY_HUB_VERSION") or _ANTIGRAVITY_HUB_VERSION_DEFAULT


async def get_antigravity_hub_cl() -> str:
    """hub UA 的 CL（changelist）。面板配置 > ENV > 默认。"""
    value = await get_config_value("antigravity_hub_cl")
    if value:
        return str(value)
    return os.getenv("ANTIGRAVITY_HUB_CL") or _ANTIGRAVITY_HUB_CL_DEFAULT


def get_antigravity_hub_version_sync() -> str:
    """同步版（UA 构建等热路径用）：面板配置缓存 > ENV > 默认。"""
    cached = _get_cached_config("antigravity_hub_version")
    if cached:
        return str(cached)
    return os.getenv("ANTIGRAVITY_HUB_VERSION") or _ANTIGRAVITY_HUB_VERSION_DEFAULT


def get_antigravity_hub_cl_sync() -> str:
    """同步版（UA 构建等热路径用）：面板配置缓存 > ENV > 默认。"""
    cached = _get_cached_config("antigravity_hub_cl")
    if cached:
        return str(cached)
    return os.getenv("ANTIGRAVITY_HUB_CL") or _ANTIGRAVITY_HUB_CL_DEFAULT


# Antigravity 请求节奏与 429 治理（2026-10-21）。与 hub 指纹同理，这些策略参数
# 走 面板配置 > ENV > 默认值，便于随时调参不调部署。
_ANTIGRAVITY_PACING_ENABLED_DEFAULT = True
# 2026-10-22：节奏控制从「每账号硬间隔」改为「并发闸门 + 毫秒级错峰」。
# 基准 1500ms 会把单账号锁成 0.67 rps（15 并发完全用不上），业务侧表现为
# 每个请求先随机等 1~2 秒；现在基准只用于打散瞬时齐发。
_ANTIGRAVITY_PACING_BASE_MS_DEFAULT = 80
_ANTIGRAVITY_PACING_JITTER_PCT_DEFAULT = 50
_ANTIGRAVITY_PACING_CONCURRENCY_DEFAULT = 15
_ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS_DEFAULT = 30.0
_ANTIGRAVITY_QUOTA_GUARD_ENABLED_DEFAULT = True
_ANTIGRAVITY_QUOTA_GUARD_THRESHOLD_DEFAULT = 0.05


def _sync_bool(key: str, env_var: str, default: bool) -> bool:
    cached = _get_cached_config(key)
    if cached is not None:
        return bool(cached)
    env_value = os.getenv(env_var)
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return default


def _sync_number(key: str, env_var: str, default: float, cast=float) -> float:
    cached = _get_cached_config(key)
    if cached is not None:
        try:
            return cast(cached)
        except (TypeError, ValueError):
            return default
    env_value = os.getenv(env_var)
    if env_value:
        try:
            return cast(env_value)
        except (TypeError, ValueError):
            return default
    return default


async def _async_panel_first(key: str, env_var: str, default: Any) -> Any:
    """面板 > ENV > 默认 的通用异步读取。"""
    value = await get_config_value(key)
    if value is not None:
        return value
    env_value = os.getenv(env_var)
    if env_value is not None and env_value != "":
        return env_value
    return default


async def get_antigravity_pacing_enabled() -> bool:
    """每账号请求节奏（随机抖动）开关。默认开启。"""
    value = await _async_panel_first(
        "antigravity_pacing_enabled", "ANTIGRAVITY_PACING_ENABLED", None
    )
    if value is None:
        return _ANTIGRAVITY_PACING_ENABLED_DEFAULT
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


def get_antigravity_pacing_enabled_sync() -> bool:
    return _sync_bool(
        "antigravity_pacing_enabled",
        "ANTIGRAVITY_PACING_ENABLED",
        _ANTIGRAVITY_PACING_ENABLED_DEFAULT,
    )


async def get_antigravity_pacing_base_ms() -> int:
    """每账号最小请求间隔基准（毫秒）。"""
    value = await _async_panel_first(
        "antigravity_pacing_base_ms",
        "ANTIGRAVITY_PACING_BASE_MS",
        _ANTIGRAVITY_PACING_BASE_MS_DEFAULT,
    )
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_PACING_BASE_MS_DEFAULT


def get_antigravity_pacing_base_ms_sync() -> int:
    return int(
        _sync_number(
            "antigravity_pacing_base_ms",
            "ANTIGRAVITY_PACING_BASE_MS",
            _ANTIGRAVITY_PACING_BASE_MS_DEFAULT,
            cast=int,
        )
    )


async def get_antigravity_pacing_jitter_pct() -> int:
    """间隔抖动幅度（百分比，±pct 均匀随机）。"""
    value = await _async_panel_first(
        "antigravity_pacing_jitter_pct",
        "ANTIGRAVITY_PACING_JITTER_PCT",
        _ANTIGRAVITY_PACING_JITTER_PCT_DEFAULT,
    )
    try:
        return min(max(int(value), 0), 95)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_PACING_JITTER_PCT_DEFAULT


def get_antigravity_pacing_jitter_pct_sync() -> int:
    return int(
        _sync_number(
            "antigravity_pacing_jitter_pct",
            "ANTIGRAVITY_PACING_JITTER_PCT",
            _ANTIGRAVITY_PACING_JITTER_PCT_DEFAULT,
            cast=int,
        )
    )


async def get_antigravity_pacing_concurrency() -> int:
    """每账号并发闸门上限（在飞上游请求数）。默认 15。"""
    value = await _async_panel_first(
        "antigravity_pacing_concurrency",
        "ANTIGRAVITY_PACING_CONCURRENCY",
        _ANTIGRAVITY_PACING_CONCURRENCY_DEFAULT,
    )
    try:
        return min(max(int(value), 1), 64)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_PACING_CONCURRENCY_DEFAULT


def get_antigravity_pacing_concurrency_sync() -> int:
    return min(
        max(
            int(
                _sync_number(
                    "antigravity_pacing_concurrency",
                    "ANTIGRAVITY_PACING_CONCURRENCY",
                    _ANTIGRAVITY_PACING_CONCURRENCY_DEFAULT,
                    cast=int,
                )
            ),
            1,
        ),
        64,
    )


async def get_antigravity_pacing_slot_wait_seconds() -> float:
    """并发占满时等待释放的上限（秒），超时放行并告警。默认 30。"""
    value = await _async_panel_first(
        "antigravity_pacing_slot_wait_seconds",
        "ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS",
        _ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS_DEFAULT,
    )
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS_DEFAULT


def get_antigravity_pacing_slot_wait_seconds_sync() -> float:
    return max(
        _sync_number(
            "antigravity_pacing_slot_wait_seconds",
            "ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS",
            _ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS_DEFAULT,
        ),
        0.0,
    )


async def get_antigravity_quota_guard_enabled() -> bool:
    """quotaInfo 主动避让开关：额度剩余低于阈值时提前换号。默认开启。"""
    value = await _async_panel_first(
        "antigravity_quota_guard_enabled", "ANTIGRAVITY_QUOTA_GUARD_ENABLED", None
    )
    if value is None:
        return _ANTIGRAVITY_QUOTA_GUARD_ENABLED_DEFAULT
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


async def get_antigravity_quota_guard_threshold() -> float:
    """quotaInfo 主动避让阈值（remainingFraction 低于该值视为额度将尽）。"""
    value = await _async_panel_first(
        "antigravity_quota_guard_threshold",
        "ANTIGRAVITY_QUOTA_GUARD_THRESHOLD",
        _ANTIGRAVITY_QUOTA_GUARD_THRESHOLD_DEFAULT,
    )
    try:
        return min(max(float(value), 0.0), 0.5)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_QUOTA_GUARD_THRESHOLD_DEFAULT


async def get_enable_geminicli() -> bool:
    """GeminiCLI is opt-in in the SQLite single-database edition."""
    env_value = os.getenv("ENABLE_GEMINICLI")
    if env_value is not None:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("enable_geminicli", False))


async def get_max_non_stream_buffer_bytes() -> int:
    value = os.getenv("MAX_NON_STREAM_BUFFER_BYTES")
    if value:
        try:
            return max(int(value), 1)
        except ValueError:
            pass
    return int(await get_config_value("max_non_stream_buffer_bytes", 16 * 1024 * 1024))


async def get_credential_candidate_limit() -> int:
    value = os.getenv("CREDENTIAL_CANDIDATE_LIMIT")
    if value:
        try:
            return min(max(int(value), 1), 32)
        except ValueError:
            pass
    return min(max(int(await get_config_value("credential_candidate_limit", 32)), 1), 32)


async def get_antigravity_switch_credential_enabled() -> bool:
    """
    Get antigravity switch credential setting.

    控制antigravity在重试时是否切换凭证。
    禁用时会持续使用当前凭证，直到该凭证对当前模型进入CD或被禁用。

    Environment variable: ANTIGRAVITY_SWITCH_CREDENTIAL
    Database config key: antigravity_switch_credential_enabled
    Default: False
    """
    env_value = os.getenv("ANTIGRAVITY_SWITCH_CREDENTIAL")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("antigravity_switch_credential_enabled", False))


# 配额耗尽时的默认模型降级链（逗号分隔），默认空 = 关闭自动降级
DEFAULT_ANTIGRAVITY_MODEL_FALLBACK_CHAIN = ""


async def get_antigravity_model_fallback_chain() -> list:
    """
    Get antigravity model fallback chain.

    模型配额耗尽（429 且带明确配额重置信息）且号池无可用凭证时，
    按链顺序降级到下一个模型重试。逗号分隔，留空关闭。

    Environment variable: ANTIGRAVITY_MODEL_FALLBACK_CHAIN
    Database config key: antigravity_model_fallback_chain
    Default: 空（关闭降级；如需启用请显式配置，如 gemini-3.8-flash,gemini-3.8-flash-tiered）
    """
    env_value = os.getenv("ANTIGRAVITY_MODEL_FALLBACK_CHAIN")
    if env_value is not None:
        raw = env_value
    else:
        raw = await get_config_value(
            "antigravity_model_fallback_chain", DEFAULT_ANTIGRAVITY_MODEL_FALLBACK_CHAIN
        )

    if not isinstance(raw, str):
        return []

    chain = []
    for item in raw.split(","):
        model = item.strip()
        if model and model not in chain:
            chain.append(model)
    return chain


async def get_oauth_proxy_url() -> str:
    """
    Get OAuth proxy URL setting.

    用于Google OAuth2认证的代理URL。

    Environment variable: OAUTH_PROXY_URL
    Database config key: oauth_proxy_url
    Default: https://oauth2.googleapis.com
    """
    return str(
        await get_config_value(
            "oauth_proxy_url", "https://oauth2.googleapis.com", "OAUTH_PROXY_URL"
        )
    )


async def get_googleapis_proxy_url() -> str:
    """
    Get Google APIs proxy URL setting.

    用于Google APIs调用的代理URL。

    Environment variable: GOOGLEAPIS_PROXY_URL
    Database config key: googleapis_proxy_url
    Default: https://www.googleapis.com
    """
    return str(
        await get_config_value(
            "googleapis_proxy_url", "https://www.googleapis.com", "GOOGLEAPIS_PROXY_URL"
        )
    )


async def get_resource_manager_api_url() -> str:
    """
    Get Google Cloud Resource Manager API URL setting.

    用于Google Cloud Resource Manager API的URL。

    Environment variable: RESOURCE_MANAGER_API_URL
    Database config key: resource_manager_api_url
    Default: https://cloudresourcemanager.googleapis.com
    """
    return str(
        await get_config_value(
            "resource_manager_api_url",
            "https://cloudresourcemanager.googleapis.com",
            "RESOURCE_MANAGER_API_URL",
        )
    )


async def get_service_usage_api_url() -> str:
    """
    Get Google Cloud Service Usage API URL setting.

    用于Google Cloud Service Usage API的URL。

    Environment variable: SERVICE_USAGE_API_URL
    Database config key: service_usage_api_url
    Default: https://serviceusage.googleapis.com
    """
    return str(
        await get_config_value(
            "service_usage_api_url", "https://serviceusage.googleapis.com", "SERVICE_USAGE_API_URL"
        )
    )


# Antigravity 默认上游地址与三 host 降级顺序
# （对齐 Antigravity-Manager：sandbox → daily → prod）
ANTIGRAVITY_DEFAULT_API_URL = "https://daily-cloudcode-pa.googleapis.com"
ANTIGRAVITY_PROD_API_URL = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_API_URL_FALLBACKS = [
    "https://daily-cloudcode-pa.sandbox.googleapis.com",
    "https://daily-cloudcode-pa.googleapis.com",
    ANTIGRAVITY_PROD_API_URL,
]


async def get_antigravity_api_url() -> str:
    """
    Get Antigravity API URL setting.

    用于Google Antigravity API的URL。

    Environment variable: ANTIGRAVITY_API_URL
    Database config key: antigravity_api_url
    Default: https://daily-cloudcode-pa.googleapis.com
    """
    return str(
        await get_config_value(
            "antigravity_api_url",
            ANTIGRAVITY_DEFAULT_API_URL,
            "ANTIGRAVITY_API_URL",
        )
    )


def antigravity_host_candidates(api_url: str) -> list:
    """
    给定 Antigravity 基础 URL，返回降级候选 host 列表。

    仅当是默认 daily 地址时返回 [sandbox, daily, prod]；
    用户配置了自定义 URL（如反代）时只返回该地址，不做降级。
    """
    if (api_url or "").rstrip("/") == ANTIGRAVITY_DEFAULT_API_URL:
        return list(ANTIGRAVITY_API_URL_FALLBACKS)
    return [api_url]


async def get_antigravity_api_url_candidates() -> list:
    """
    当前配置下的 Antigravity 降级候选 host 列表（用于轻量端点重试）。
    """
    return antigravity_host_candidates(await get_antigravity_api_url())


async def get_antigravity_network_check_enabled() -> bool:
    """Return whether account-bound IP and region checks gate requests."""
    env_value = os.getenv("ANTIGRAVITY_NETWORK_CHECK_ENABLED")
    if env_value is not None:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("antigravity_network_check_enabled", False))


async def get_antigravity_network_check_ttl_seconds() -> int:
    """Return the maximum age of a successful account network check."""
    env_value = os.getenv("ANTIGRAVITY_NETWORK_CHECK_TTL_SECONDS")
    if env_value:
        try:
            return max(int(env_value), 60)
        except ValueError:
            pass
    value = await get_config_value("antigravity_network_check_ttl_seconds", 1800)
    try:
        return max(int(value), 60)
    except (TypeError, ValueError):
        return 1800


async def get_antigravity_egress_ip_check_url() -> str:
    """Return the operator-provided IP echo endpoint; empty means unconfigured."""
    return str(
        await get_config_value(
            "antigravity_egress_ip_check_url",
            "",
            "ANTIGRAVITY_EGRESS_IP_CHECK_URL",
        )
        or ""
    ).strip()


async def get_antigravity_403_recheck_enabled() -> bool:
    """Return whether 403-banned antigravity accounts are periodically rechecked."""
    env_value = os.getenv("ANTIGRAVITY_403_RECHECK_ENABLED")
    if env_value is not None:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("antigravity_403_recheck_enabled", True))


async def get_antigravity_403_recheck_interval() -> int:
    """Return the interval (seconds) between 403 recheck rounds."""
    env_value = os.getenv("ANTIGRAVITY_403_RECHECK_INTERVAL")
    if env_value:
        try:
            return max(int(env_value), 60)
        except ValueError:
            pass
    value = await get_config_value("antigravity_403_recheck_interval", 3600)
    try:
        return max(int(value), 60)
    except (TypeError, ValueError):
        return 3600


async def get_keepalive_url() -> str:
    """
    Get keep-alive URL setting.

    配置后保活服务会定期向该URL发送GET请求。
    留空表示禁用保活服务。

    Environment variable: KEEPALIVE_URL
    Database config key: keepalive_url
    Default: "" (disabled)
    """
    return str(await get_config_value("keepalive_url", "", "KEEPALIVE_URL"))


async def get_keepalive_interval() -> int:
    """
    Get keep-alive interval in seconds.

    保活请求发送间隔（秒）。

    Environment variable: KEEPALIVE_INTERVAL
    Database config key: keepalive_interval
    Default: 60
    """
    env_value = os.getenv("KEEPALIVE_INTERVAL")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("keepalive_interval", 60))


async def get_call_records_retention_days() -> int:
    """
    调用监控逐条记录的滚动保留天数，超过的记录会被自动清理。

    Environment variable: CALL_RECORDS_RETENTION_DAYS
    Database config key: call_records_retention_days
    Default: 30（范围 1-365）
    """
    env_value = os.getenv("CALL_RECORDS_RETENTION_DAYS")
    if env_value:
        try:
            return min(max(int(env_value), 1), 365)
        except ValueError:
            pass
    value = await get_config_value("call_records_retention_days", 30)
    try:
        return min(max(int(value), 1), 365)
    except (TypeError, ValueError):
        return 30
