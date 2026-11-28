"""
Configuration constants for the Geminicli2api proxy server.
Centralizes all configuration to avoid duplication across modules.

- 启动时加载一次配置到内存
- 修改配置时调用 reload_config() 重新从数据库加载
"""

import math
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
    "ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE": "antigravity_system_obfuscation_mode",
    "ANTIGRAVITY_SYSTEM_PROMPT_ENABLED": "antigravity_system_prompt_enabled",
    "ANTIGRAVITY_OFFICIAL_TOOL_ALIGNMENT": "antigravity_official_tool_alignment_enabled",
    "ANTIGRAVITY_SYSTEM_AS_USER_ENABLED": "antigravity_system_as_user_enabled",
    "ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED": "antigravity_system_vowel_obfuscation_enabled",
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
    "SENSITIVE_WORD_FILTER_MODE": "sensitive_word_filter_mode",
    "SENSITIVE_WORD_USE_DEFAULT_KEYWORDS": "sensitive_word_use_default_keywords",
    "COMPATIBILITY_MODE": "compatibility_mode_enabled",
    "RETURN_THOUGHTS_TO_FRONTEND": "return_thoughts_to_frontend",
    "ANTIGRAVITY_SWITCH_CREDENTIAL": "antigravity_switch_credential_enabled",
    "ANTIGRAVITY_MODEL_FALLBACK_CHAIN": "antigravity_model_fallback_chain",
    "ANTIGRAVITY_TTFT_FALLBACK_SECONDS": "antigravity_ttft_fallback_seconds",
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
    "REMOTE_IMAGE_INLINE_MODE": "remote_image_inline_mode",
    "REMOTE_IMAGE_INLINE_IPS": "remote_image_inline_ips",
    "REMOTE_IMAGE_INLINE_TRUST_FORWARDED": "remote_image_inline_trust_forwarded",
}

ENV_MAPPINGS["CALL_CONTENT_CAPTURE_ENABLED"] = "call_content_capture_enabled"
ENV_MAPPINGS["ANTIGRAVITY_RELAY_HOLD_SECONDS"] = "antigravity_relay_hold_seconds"

# ====================== 远程图片内联策略 ======================
# all: 所有第三方 http(s) 图片一律下载后内联（历史默认）
# ip_list: 仅当请求来源 IP 命中白名单时内联，其余原样透传 fileData
# off: 一律不内联，全部透传
REMOTE_IMAGE_INLINE_MODES = ("all", "ip_list", "off")
DEFAULT_REMOTE_IMAGE_INLINE_MODE = "all"
MAX_REMOTE_IMAGE_INLINE_IPS = 512


# ====================== 配置系统 ======================

async def init_config():
    """初始化配置缓存（启动时调用一次）"""
    global _config_cache, _config_initialized

    if _config_initialized:
        return

    try:
        from src.storage_adapter import get_storage_adapter
        storage_adapter = await get_storage_adapter()
        if not os.getenv('ANTIGRAVITY_SYSTEM_PROMPT_ENABLED'):
            try:
                await storage_adapter.migrate_agy_base_prompt_default()
            except Exception as exc:
                from log import log
                log.warning(f'AGY 基础提示默认值迁移失败，下次启动重试: {exc}')
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


def _inline_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in REMOTE_IMAGE_INLINE_MODES else DEFAULT_REMOTE_IMAGE_INLINE_MODE


def _inline_ips(value: Any) -> list:
    if isinstance(value, str):
        items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return []
    return [item.strip() for item in items if str(item).strip()]


async def get_remote_image_inline_mode() -> str:
    """Get remote image inline policy mode: all / ip_list / off."""
    env_value = os.getenv("REMOTE_IMAGE_INLINE_MODE")
    if env_value:
        return _inline_mode(env_value)
    return _inline_mode(await get_config_value("remote_image_inline_mode"))


def get_remote_image_inline_mode_sync() -> str:
    """Read the inline mode from the loaded config cache (hot path safe)."""
    env_value = os.getenv("REMOTE_IMAGE_INLINE_MODE")
    if env_value:
        return _inline_mode(env_value)
    return _inline_mode(_get_cached_config("remote_image_inline_mode"))


async def get_remote_image_inline_ips() -> list:
    """Get the client IP allowlist used by the ip_list inline mode."""
    env_value = os.getenv("REMOTE_IMAGE_INLINE_IPS")
    if env_value:
        return _inline_ips(env_value)
    return _inline_ips(await get_config_value("remote_image_inline_ips"))


def get_remote_image_inline_ips_sync() -> list:
    env_value = os.getenv("REMOTE_IMAGE_INLINE_IPS")
    if env_value:
        return _inline_ips(env_value)
    return _inline_ips(_get_cached_config("remote_image_inline_ips"))


async def get_remote_image_inline_trust_forwarded() -> bool:
    """Trust X-Forwarded-For / X-Real-IP when resolving the client IP."""
    env_value = os.getenv("REMOTE_IMAGE_INLINE_TRUST_FORWARDED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("remote_image_inline_trust_forwarded", False))


def get_remote_image_inline_trust_forwarded_sync() -> bool:
    env_value = os.getenv("REMOTE_IMAGE_INLINE_TRUST_FORWARDED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return bool(_get_cached_config("remote_image_inline_trust_forwarded", False))


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


async def get_text_account_retry_status_codes() -> list[int]:
    """读取当前文本换号规则，保留显式配置的空列表。

    Returns:
        可触发一次换号的上游 HTTP 状态码。
    """
    from src.api.text_retry import DEFAULT_STATUS_CODES, parse_status_codes

    return parse_status_codes(await get_config_value("text_account_retry_status_codes", DEFAULT_STATUS_CODES))


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

    启用后按模式过滤 system prompt 中的客户端名称；默认 rewrite 替换后放行。

    Environment variable: SENSITIVE_WORD_REPLACE_ENABLED
    Database config key: sensitive_word_replace_enabled
    Default: True
    """
    env_value = os.getenv("SENSITIVE_WORD_REPLACE_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("sensitive_word_replace_enabled", True))


async def get_sensitive_word_filter_mode() -> str:
    """System prompt filter: rewrite (default) or block."""
    value = str(await get_config_value(
        "sensitive_word_filter_mode", "rewrite", "SENSITIVE_WORD_FILTER_MODE"
    )).strip().lower()
    return value if value in ("rewrite", "block") else "rewrite"


async def get_sensitive_word_use_default_keywords() -> bool:
    """Enable built-in coding-client names by default."""
    value = os.getenv("SENSITIVE_WORD_USE_DEFAULT_KEYWORDS")
    if value:
        return value.lower() in ("true", "1", "yes", "on")
    return bool(await get_config_value("sensitive_word_use_default_keywords", True))


async def get_sensitive_word_replace_rules() -> str:
    """
    Get sensitive word replacement rules text.

    每行一条规则，格式为 "查找词=>替换词"；无 "=>" 时替换为空（即删除）；
    以 "#" 开头的行为注释；匹配不区分大小写，长词优先。

    Environment variable: SENSITIVE_WORD_REPLACE_RULES
    Database config key: sensitive_word_replace_rules
    Default: "" (无自定义规则；内置词库由独立开关控制)
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
def get_server_keep_alive_timeout() -> float:
    """Startup-only idle connection timeout; ENV > default, in both worker modes."""
    raw = os.getenv("HTTP_KEEP_ALIVE_TIMEOUT", "").strip() or "60"
    try:
        timeout = float(raw)
    except ValueError:
        raise ValueError("HTTP_KEEP_ALIVE_TIMEOUT 必须是大于 0 的有限秒数") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("HTTP_KEEP_ALIVE_TIMEOUT 必须是大于 0 的有限秒数")
    return timeout


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
    """兼容旧调用方；上游协议始终跟随客户端。"""
    return False


# Antigravity hub 轨迹指纹（2.35.2 / CL 1029311855，官方二进制实读核验，2026-11-26）
# 这两个键刻意不走 ENV_MAPPINGS：优先级为 面板配置 > ENV > 默认值，
# ENV 只作部署期兜底初始值，面板随时可改（hub 通道周更，需频繁维护）。
_ANTIGRAVITY_HUB_VERSION_DEFAULT = "2.35.2"
_ANTIGRAVITY_HUB_CL_DEFAULT = "1029311855"


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
# 2026-11-25：账号级「动态最小间隔」。启用后每次上游尝试前按 [min, max] 秒
# 均匀随机等待（再乘该账号的节奏人格系数），把单账号请求密度压到接近真人终端；
# 0 表示不启用。均值 6s 时单账号 ≈0.17 rps，吞吐按比例下降，按业务需要显式开启。
_ANTIGRAVITY_PACING_MIN_INTERVAL_SECONDS_DEFAULT = 0.0
_ANTIGRAVITY_PACING_MAX_INTERVAL_SECONDS_DEFAULT = 0.0
_ANTIGRAVITY_PACING_CONCURRENCY_DEFAULT = 15
_ANTIGRAVITY_PACING_SLOT_WAIT_SECONDS_DEFAULT = 30.0
# 账号静默窗口：每个账号每天有一段不参与调度的时段，按账号名稳定分配。
# 目的不是省额度，而是让整池不再呈现「24 小时恒定在线」的机器画像。
_ANTIGRAVITY_DORMANT_ENABLED_DEFAULT = False
_ANTIGRAVITY_DORMANT_HOURS_DEFAULT = 6
# 全池闸门：极端负载下先在闸前排队吸收，排不下才快速失败并给出诚实 Retry-After。
# 默认关闭；0 表示该项不限制。
# PX6 代理池定时同步：默认开启，间隔 1 小时；组未指定时按名称含 PX6 自动识别。
_PX6_REFRESH_ENABLED_DEFAULT = True
_PX6_REFRESH_INTERVAL_DEFAULT = 3600
_PX6_GROUP_ID_DEFAULT: Optional[int] = None

_ANTIGRAVITY_FLEET_ENABLED_DEFAULT = False
_ANTIGRAVITY_FLEET_INFLIGHT_DEFAULT = 0
_ANTIGRAVITY_FLEET_RPM_DEFAULT = 0
_ANTIGRAVITY_FLEET_WAIT_SECONDS_DEFAULT = 60.0
_ANTIGRAVITY_QUOTA_GUARD_ENABLED_DEFAULT = True
_ANTIGRAVITY_QUOTA_GUARD_THRESHOLD_DEFAULT = 0.05
# 额度/档位低频对账：整池 10 分钟一轮、每轮 2 个号、同一账号 1 小时内只探一次。
# 整池租约将多 worker 的探测速率合计限制为最多 12 次/小时；游标轮转覆盖账号池。
_ANTIGRAVITY_QUOTA_SWEEP_ENABLED_DEFAULT = True
_ANTIGRAVITY_QUOTA_SWEEP_INTERVAL_SECONDS_DEFAULT = 600
_ANTIGRAVITY_QUOTA_SWEEP_BATCH_DEFAULT = 2
_ANTIGRAVITY_QUOTA_SWEEP_ACCOUNT_INTERVAL_SECONDS_DEFAULT = 3600


def _sync_bool(key: str, env_var: str, default: bool) -> bool:
    cached = _get_cached_config(key)
    if cached is not None:
        return bool(cached)
    env_value = os.getenv(env_var)
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")
    return default


def get_antigravity_native_transport_enabled_sync() -> bool:
    return _sync_bool(
        "antigravity_native_transport_enabled",
        "ANTIGRAVITY_NATIVE_TRANSPORT_ENABLED",
        False,
    )


async def get_antigravity_native_transport_enabled() -> bool:
    return get_antigravity_native_transport_enabled_sync()


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


async def get_antigravity_pacing_min_interval_seconds() -> float:
    """账号级动态最小间隔下限（秒）。0 表示不启用。"""
    value = await _async_panel_first(
        "antigravity_pacing_min_interval_seconds",
        "ANTIGRAVITY_PACING_MIN_INTERVAL_SECONDS",
        _ANTIGRAVITY_PACING_MIN_INTERVAL_SECONDS_DEFAULT,
    )
    try:
        return min(max(float(value), 0.0), 600.0)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_PACING_MIN_INTERVAL_SECONDS_DEFAULT


def get_antigravity_pacing_min_interval_seconds_sync() -> float:
    return min(
        max(
            _sync_number(
                "antigravity_pacing_min_interval_seconds",
                "ANTIGRAVITY_PACING_MIN_INTERVAL_SECONDS",
                _ANTIGRAVITY_PACING_MIN_INTERVAL_SECONDS_DEFAULT,
            ),
            0.0,
        ),
        600.0,
    )


async def get_antigravity_pacing_max_interval_seconds() -> float:
    """账号级动态最小间隔上限（秒）。0 表示不启用。"""
    value = await _async_panel_first(
        "antigravity_pacing_max_interval_seconds",
        "ANTIGRAVITY_PACING_MAX_INTERVAL_SECONDS",
        _ANTIGRAVITY_PACING_MAX_INTERVAL_SECONDS_DEFAULT,
    )
    try:
        return min(max(float(value), 0.0), 600.0)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_PACING_MAX_INTERVAL_SECONDS_DEFAULT


def get_antigravity_pacing_max_interval_seconds_sync() -> float:
    return min(
        max(
            _sync_number(
                "antigravity_pacing_max_interval_seconds",
                "ANTIGRAVITY_PACING_MAX_INTERVAL_SECONDS",
                _ANTIGRAVITY_PACING_MAX_INTERVAL_SECONDS_DEFAULT,
            ),
            0.0,
        ),
        600.0,
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


def get_antigravity_account_concurrency(credential_name: str) -> dict:
    """Account overrides; absent request limit follows global pacing."""
    raw = _get_cached_config(f"antigravity_account_concurrency:{credential_name}")
    raw = raw if isinstance(raw, dict) else {}
    result = {}
    for key, default in (
        ("request_concurrency", get_antigravity_pacing_concurrency_sync()),
        ("image_task_concurrency", 1),
    ):
        value = raw.get(key)
        try:
            result[key] = min(max(int(value), 1), 64) if value is not None else default
        except (TypeError, ValueError):
            result[key] = default
    result["request_concurrency_override"] = raw.get("request_concurrency")
    return result


async def get_antigravity_pacing_slot_wait_seconds() -> float:
    """并发占满时等待释放的上限（秒），超时返回 429 背压并告警。默认 30。"""
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


async def get_antigravity_dormant_enabled() -> bool:
    """账号每日静默窗口开关。默认关闭，需显式打开。"""
    value = await _async_panel_first(
        "antigravity_dormant_enabled", "ANTIGRAVITY_DORMANT_ENABLED", None
    )
    if value is None:
        return _ANTIGRAVITY_DORMANT_ENABLED_DEFAULT
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


def get_antigravity_dormant_enabled_sync() -> bool:
    return _sync_bool(
        "antigravity_dormant_enabled",
        "ANTIGRAVITY_DORMANT_ENABLED",
        _ANTIGRAVITY_DORMANT_ENABLED_DEFAULT,
    )


async def get_antigravity_dormant_hours() -> int:
    """每个账号每天静默的小时数（0-20）。默认 6。"""
    value = await _async_panel_first(
        "antigravity_dormant_hours",
        "ANTIGRAVITY_DORMANT_HOURS",
        _ANTIGRAVITY_DORMANT_HOURS_DEFAULT,
    )
    try:
        return min(max(int(value), 0), 20)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_DORMANT_HOURS_DEFAULT


def get_antigravity_dormant_hours_sync() -> int:
    return min(
        max(
            int(
                _sync_number(
                    "antigravity_dormant_hours",
                    "ANTIGRAVITY_DORMANT_HOURS",
                    _ANTIGRAVITY_DORMANT_HOURS_DEFAULT,
                    cast=int,
                )
            ),
            0,
        ),
        20,
    )


async def get_px6_refresh_enabled() -> bool:
    """PX6 代理池定时同步开关。默认开启。"""
    value = await _async_panel_first("px6_refresh_enabled", "PX6_REFRESH_ENABLED", None)
    if value is None:
        return _PX6_REFRESH_ENABLED_DEFAULT
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


async def get_px6_refresh_interval() -> int:
    """同步间隔（秒），默认 3600。"""
    value = await _async_panel_first("px6_refresh_interval", "PX6_REFRESH_INTERVAL", None)
    try:
        return min(max(int(value), 60), 86400)
    except (TypeError, ValueError):
        return _PX6_REFRESH_INTERVAL_DEFAULT


async def get_px6_group_id() -> Optional[int]:
    """要维护的代理组 id；未配置时返回 None（由调用方按名称识别）。"""
    value = await _async_panel_first("px6_group_id", "PX6_GROUP_ID", None)
    if value in (None, ""):
        return _PX6_GROUP_ID_DEFAULT
    try:
        return int(value)
    except (TypeError, ValueError):
        return _PX6_GROUP_ID_DEFAULT


async def get_antigravity_fleet_enabled() -> bool:
    """全池闸门开关。默认关闭。"""
    value = await _async_panel_first(
        "antigravity_fleet_enabled", "ANTIGRAVITY_FLEET_ENABLED", None
    )
    if value is None:
        return _ANTIGRAVITY_FLEET_ENABLED_DEFAULT
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


def get_antigravity_fleet_enabled_sync() -> bool:
    return _sync_bool(
        "antigravity_fleet_enabled",
        "ANTIGRAVITY_FLEET_ENABLED",
        _ANTIGRAVITY_FLEET_ENABLED_DEFAULT,
    )


async def get_antigravity_fleet_inflight() -> int:
    """全池在飞并发上限；0 表示不限制。"""
    value = await _async_panel_first(
        "antigravity_fleet_inflight", "ANTIGRAVITY_FLEET_INFLIGHT", None
    )
    try:
        return min(max(int(value), 0), 4096)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_FLEET_INFLIGHT_DEFAULT


def get_antigravity_fleet_inflight_sync() -> int:
    return min(
        max(
            int(
                _sync_number(
                    "antigravity_fleet_inflight",
                    "ANTIGRAVITY_FLEET_INFLIGHT",
                    _ANTIGRAVITY_FLEET_INFLIGHT_DEFAULT,
                    cast=int,
                )
            ),
            0,
        ),
        4096,
    )


async def get_antigravity_fleet_rpm() -> int:
    """全池每分钟请求上限；0 表示不限制。"""
    value = await _async_panel_first(
        "antigravity_fleet_rpm", "ANTIGRAVITY_FLEET_RPM", None
    )
    try:
        return min(max(int(value), 0), 100000)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_FLEET_RPM_DEFAULT


def get_antigravity_fleet_rpm_sync() -> int:
    return min(
        max(
            int(
                _sync_number(
                    "antigravity_fleet_rpm",
                    "ANTIGRAVITY_FLEET_RPM",
                    _ANTIGRAVITY_FLEET_RPM_DEFAULT,
                    cast=int,
                )
            ),
            0,
        ),
        100000,
    )


async def get_antigravity_fleet_wait_seconds() -> float:
    """闸门前的有界等待上限（秒）。默认 60。"""
    value = await _async_panel_first(
        "antigravity_fleet_wait_seconds",
        "ANTIGRAVITY_FLEET_WAIT_SECONDS",
        _ANTIGRAVITY_FLEET_WAIT_SECONDS_DEFAULT,
    )
    try:
        return min(max(float(value), 0.0), 600.0)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_FLEET_WAIT_SECONDS_DEFAULT


def get_antigravity_fleet_wait_seconds_sync() -> float:
    return min(
        max(
            _sync_number(
                "antigravity_fleet_wait_seconds",
                "ANTIGRAVITY_FLEET_WAIT_SECONDS",
                _ANTIGRAVITY_FLEET_WAIT_SECONDS_DEFAULT,
            ),
            0.0,
        ),
        600.0,
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


async def get_antigravity_quota_sweep_enabled() -> bool:
    """额度/档位低频对账开关。默认开启。"""
    value = await _async_panel_first(
        "antigravity_quota_sweep_enabled", "ANTIGRAVITY_QUOTA_SWEEP_ENABLED", None
    )
    if value is None:
        return _ANTIGRAVITY_QUOTA_SWEEP_ENABLED_DEFAULT
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


def get_antigravity_quota_sweep_enabled_sync() -> bool:
    return _sync_bool(
        "antigravity_quota_sweep_enabled",
        "ANTIGRAVITY_QUOTA_SWEEP_ENABLED",
        _ANTIGRAVITY_QUOTA_SWEEP_ENABLED_DEFAULT,
    )


async def get_antigravity_quota_sweep_interval_seconds() -> float:
    """两轮扫描的间隔（秒），下限 60s，避免被误配成高频刷新。"""
    value = await _async_panel_first(
        "antigravity_quota_sweep_interval_seconds",
        "ANTIGRAVITY_QUOTA_SWEEP_INTERVAL_SECONDS",
        _ANTIGRAVITY_QUOTA_SWEEP_INTERVAL_SECONDS_DEFAULT,
    )
    try:
        return min(max(float(value), 60.0), 24 * 3600.0)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_QUOTA_SWEEP_INTERVAL_SECONDS_DEFAULT


async def get_antigravity_quota_sweep_batch() -> int:
    """每轮最多探测的账号数，上限 10。"""
    value = await _async_panel_first(
        "antigravity_quota_sweep_batch",
        "ANTIGRAVITY_QUOTA_SWEEP_BATCH",
        _ANTIGRAVITY_QUOTA_SWEEP_BATCH_DEFAULT,
    )
    try:
        return min(max(int(value), 1), 10)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_QUOTA_SWEEP_BATCH_DEFAULT


async def get_antigravity_quota_sweep_account_interval_seconds() -> float:
    """同一账号两次探测的最小间隔（秒），下限 1h。"""
    value = await _async_panel_first(
        "antigravity_quota_sweep_account_interval_seconds",
        "ANTIGRAVITY_QUOTA_SWEEP_ACCOUNT_INTERVAL_SECONDS",
        _ANTIGRAVITY_QUOTA_SWEEP_ACCOUNT_INTERVAL_SECONDS_DEFAULT,
    )
    try:
        return min(max(float(value), 3600.0), 7 * 24 * 3600.0)
    except (TypeError, ValueError):
        return _ANTIGRAVITY_QUOTA_SWEEP_ACCOUNT_INTERVAL_SECONDS_DEFAULT


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


# 发送前无凭证和首次有效响应超时共用的模型链，默认留空关闭。
DEFAULT_ANTIGRAVITY_MODEL_FALLBACK_CHAIN = ""


async def get_antigravity_ttft_fallback_seconds() -> int:
    """返回首次有效响应等待秒数；0 只关闭首次有效响应超时降级。

    Returns:
        当前阈值，无效配置使用默认值 30。
    """
    value = await get_config_value(
        "antigravity_ttft_fallback_seconds", 30, "ANTIGRAVITY_TTFT_FALLBACK_SECONDS"
    )
    try:
        seconds = int(str(value))
        return seconds if 0 <= seconds <= 86400 else 30
    except ValueError:
        return 30


async def get_antigravity_model_fallback_chain() -> list:
    """
    Get antigravity model fallback chain.

    无可用凭证或首次有效响应超时时，只能切到链内下一项。逗号分隔，留空关闭。

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


async def get_antigravity_relay_hold_seconds() -> int:
    """侧车合成 502（上游未应答）的就地冷却时长（秒）。

    这类失败只说明这一跳没谈成：同一账号同一节点在前后一秒仍可能返回 200。
    因此窗口远短于上游 5xx 的半天，并允许在面板「凭证与重试」里调整。
    """
    env_value = os.getenv("ANTIGRAVITY_RELAY_HOLD_SECONDS")
    source = env_value if env_value not in (None, "") else None
    if source is not None:
        try:
            return max(int(source), 0)
        except ValueError:
            pass
    value = await get_config_value("antigravity_relay_hold_seconds", 60)
    try:
        seconds = int(str(value))
    except ValueError:
        return 60
    return seconds if 0 <= seconds <= 3600 else 60


async def get_antigravity_transient_hold_seconds() -> int:
    """上游 5xx 后该账号的就地冷却时长（秒）。

    500/502 使用配置时长；503 每次都冷却，按连续失败窗口从 60 秒逐级退避
    （60/120/240/480 秒）到此上限（默认 600 秒）。
    """
    env_value = os.getenv("ANTIGRAVITY_TRANSIENT_HOLD_SECONDS")
    if env_value:
        try:
            return max(int(env_value), 0)
        except ValueError:
            pass
    value = await get_config_value("antigravity_transient_hold_seconds", 600)
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 600


async def get_antigravity_session_reuse_ratio() -> float:
    """无显式 sessionId 时，按账号/调用方/模型复用上游会话的比例。

    0 表示完全保持旧行为（每个请求一条新 trajectory、step 恒为 1）。
    真实客户端既有连续对话也有新对话，因此建议灰度到 0.3–0.4 而非 1.0。
    """
    env_value = os.getenv("ANTIGRAVITY_SESSION_REUSE_RATIO")
    raw = env_value if env_value not in (None, "") else await get_config_value(
        "antigravity_session_reuse_ratio", 0.0
    )
    try:
        return min(max(float(raw), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


async def get_antigravity_session_max_steps() -> int:
    """单条 trajectory 的最大步数；超过后强制开启新会话。

    与 labels.request_id 恒定配套：恒定时单条轨迹只挂一个 request_id，
    步数上限直接决定该 id 下的调用密度，官方实抓为 1–3 步，故取 10。
    """
    env_value = os.getenv("ANTIGRAVITY_SESSION_MAX_STEPS")
    if env_value:
        try:
            return max(int(env_value), 1)
        except ValueError:
            pass
    value = await get_config_value("antigravity_session_max_steps", 10)
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return 10


async def get_antigravity_session_bucket_seconds() -> int:
    """复用会话的时间桶宽度：同一桶内的请求共享一条 trajectory。"""
    env_value = os.getenv("ANTIGRAVITY_SESSION_BUCKET_SECONDS")
    if env_value:
        try:
            return max(int(env_value), 60)
        except ValueError:
            pass
    value = await get_config_value("antigravity_session_bucket_seconds", 900)
    try:
        return max(int(value), 60)
    except (TypeError, ValueError):
        return 900


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


# DingTalk webhook 告警：SQLite 配置优先，未写入 SQLite 时回退 .env。
_DINGTALK_ALERT_CODES_DEFAULT = [429, 500, 502, 503]
_DINGTALK_ALERT_WINDOW_DEFAULT = 5
_DINGTALK_ALERT_THRESHOLD_DEFAULT = 3
_DINGTALK_ALERT_COOLDOWN_DEFAULT = 30
_DINGTALK_ALERT_TEMPLATE_DEFAULT = (
    "{keyword}告警\n状态码：{status_code}\n"
    "最近 {window_minutes} 分钟至少出现 {count} 次（阈值 {threshold} 次）\n"
    "配置返回码：{status_codes}\n通道：{channel}\n模型：{model}\n时间：{timestamp}"
)


async def get_dingtalk_webhook_url() -> str:
    value = await get_config_value("dingtalk_webhook_url", None)
    if value is not None:
        return str(value).strip()
    return os.getenv("DINGTALK_WEBHOOK_URL", "").strip()


async def get_dingtalk_alert_status_codes() -> list[int]:
    value = await get_config_value("dingtalk_alert_status_codes", None)
    if value is None:
        value = os.getenv("DINGTALK_ALERT_STATUS_CODES", ",".join(map(str, _DINGTALK_ALERT_CODES_DEFAULT)))
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value).split(",")
    result = []
    for item in raw:
        try:
            code = int(item)
        except (TypeError, ValueError):
            continue
        if 100 <= code <= 599 and code not in result:
            result.append(code)
    return result


async def _get_dingtalk_alert_int(key: str, env: str, default: int, minimum: int, maximum: int) -> int:
    value = await get_config_value(key, None)
    if value is None:
        value = os.getenv(env, str(default))
    try:
        return min(max(int(value), minimum), maximum)
    except (TypeError, ValueError):
        return default


async def get_dingtalk_alert_window_minutes() -> int:
    return await _get_dingtalk_alert_int("dingtalk_alert_window_minutes", "DINGTALK_ALERT_WINDOW_MINUTES", _DINGTALK_ALERT_WINDOW_DEFAULT, 1, 1440)


async def get_dingtalk_alert_threshold() -> int:
    return await _get_dingtalk_alert_int("dingtalk_alert_threshold", "DINGTALK_ALERT_THRESHOLD", _DINGTALK_ALERT_THRESHOLD_DEFAULT, 1, 1000)


async def get_dingtalk_alert_cooldown_minutes() -> int:
    return await _get_dingtalk_alert_int("dingtalk_alert_cooldown_minutes", "DINGTALK_ALERT_COOLDOWN_MINUTES", _DINGTALK_ALERT_COOLDOWN_DEFAULT, 0, 1440)


async def get_dingtalk_alert_template() -> str:
    value = await get_config_value("dingtalk_alert_template", None)
    if value is None:
        value = os.getenv("DINGTALK_ALERT_TEMPLATE", _DINGTALK_ALERT_TEMPLATE_DEFAULT)
    return str(value)


async def get_dingtalk_all_accounts_alert_enabled() -> bool:
    value = await get_config_value("dingtalk_all_accounts_alert_enabled", None)
    if value is None:
        value = os.getenv("DINGTALK_ALL_ACCOUNTS_ALERT_ENABLED", "true")
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "on")


async def get_dingtalk_alert_enabled() -> bool:
    value = await get_config_value("dingtalk_alert_enabled", None)
    if value is None:
        value = os.getenv("DINGTALK_ALERT_ENABLED", "false")
    return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes", "on")


async def get_dingtalk_all_accounts_alert_cooldown_minutes() -> int:
    return await _get_dingtalk_alert_int("dingtalk_all_accounts_alert_cooldown_minutes", "DINGTALK_ALL_ACCOUNTS_ALERT_COOLDOWN_MINUTES", 60, 0, 1440)


async def get_dingtalk_all_accounts_alert_template() -> str:
    value = await get_config_value("dingtalk_all_accounts_alert_template", None)
    if value is None:
        value = os.getenv("DINGTALK_ALL_ACCOUNTS_ALERT_TEMPLATE", "")
    return str(value)


async def get_system_prompt_append() -> str:
    """Custom system text; SQLite (including empty string) overrides .env."""
    value = await get_config_value("system_prompt_append", None)
    if value is None:
        value = os.getenv("SYSTEM_PROMPT_APPEND", "")
    return value if isinstance(value, str) else ""


def get_call_content_capture_enabled_sync() -> bool:
    """Capture full client/upstream snapshots; disable to keep only usage metadata."""
    return _sync_bool("call_content_capture_enabled", "CALL_CONTENT_CAPTURE_ENABLED", True)


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
        return 10


async def get_antigravity_client_identity_normalization_enabled() -> bool:
    """客户端 harness 身份归一化开关，默认开启。

    只改写 system 开头的 ``You are an AI agent powered by <品牌>.`` 身份句与
    紧随其后的宿主身份声明；标题、代码块、输出模板和业务正文保留。
    命中不到时请求完全不变，因此对官方客户端与普通业务请求零影响。

    Environment variable: ANTIGRAVITY_CLIENT_IDENTITY_NORMALIZATION
    """
    value = await get_config_value(
        'antigravity_client_identity_normalization_enabled', True,
        env_var='ANTIGRAVITY_CLIENT_IDENTITY_NORMALIZATION',
    )
    return value.lower() in ('true', '1', 'yes', 'on') if isinstance(value, str) else bool(value)


async def get_antigravity_system_prompt_enabled() -> bool:
    """AGY 基础提示注入开关，默认开启。

    注入内容固定为仓库内置的官方 Antigravity 基础模块
    （src/prompts/antigravity-<hub 版本>.txt，缺失版本回退 2.31.2），
    与自定义前置文本、提示词融合结果是三件不同的事。
    只有显式环境变量或面板保存的 false 才表示关闭。
    """
    value = await get_config_value('antigravity_system_prompt_enabled', True, env_var='ANTIGRAVITY_SYSTEM_PROMPT_ENABLED')
    return value.lower() in ('true', '1', 'yes', 'on') if isinstance(value, str) else bool(value)


async def get_antigravity_official_tool_alignment_enabled() -> bool:
    """工具声明对齐到官方 agy 2.35.2 形态的开关，默认开启。

    开启后 wrap_cli_request 会把客户端 tools 按来源工具集（AGY / DSH / Claude Code /
    GPT(Codex)）的规范顺序重排、合并成单个 functionDeclarations entry，并把 schema 的
    type/数值约束改成官方 protojson 写法。只改形态，不改工具名与参数语义。
    """
    value = await get_config_value(
        'antigravity_official_tool_alignment_enabled', True,
        env_var='ANTIGRAVITY_OFFICIAL_TOOL_ALIGNMENT',
    )
    return value.lower() in ('true', '1', 'yes', 'on') if isinstance(value, str) else bool(value)


async def get_antigravity_system_as_user_enabled() -> bool:
    value = await get_config_value('antigravity_system_as_user_enabled', False, env_var='ANTIGRAVITY_SYSTEM_AS_USER_ENABLED')
    return value.lower() in ('true', '1', 'yes', 'on') if isinstance(value, str) else bool(value)


async def get_antigravity_system_vowel_obfuscation_enabled() -> bool:
    return await get_antigravity_system_obfuscation_mode() != 'off'


DEFAULT_SYSTEM_OBFUSCATION_MODE = 'off'


async def get_antigravity_system_obfuscation_mode() -> str:
    """New mode ENV > legacy ENV > stored mode > stored legacy boolean > protected."""
    value = os.getenv('ANTIGRAVITY_SYSTEM_OBFUSCATION_MODE')
    legacy = os.getenv('ANTIGRAVITY_SYSTEM_VOWEL_OBFUSCATION_ENABLED')
    if value is None and legacy is None:
        value = await get_config_value('antigravity_system_obfuscation_mode')
        if value is None:
            legacy = await get_config_value('antigravity_system_vowel_obfuscation_enabled')
    if value is not None:
        return value if value in ('off', 'full', 'protected') else 'off'
    enabled = legacy.lower() in ('true', '1', 'yes', 'on') if isinstance(legacy, str) else legacy
    if enabled is None:
        return DEFAULT_SYSTEM_OBFUSCATION_MODE
    # 旧布尔开关显式开启时仍按“智能保护”处理，避免升级后静默降级。
    return 'protected' if enabled else 'off'
