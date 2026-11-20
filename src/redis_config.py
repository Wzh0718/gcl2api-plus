"""Safe Redis client construction from environment variables."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import parse_qsl, urlsplit


class RedisConfigurationError(ValueError):
    """Raised for invalid or ambiguous Redis environment configuration."""


# 客户端超时默认 3 秒。写死 1 秒在高并发下会误判：连接前要先做一次阻塞
# DNS 解析（asyncio 默认线程池），号池打满时排队本身就超过 1 秒，于是
# 「DNS 慢」被报成「Redis 不可用」。3 秒仍远低于任何用户可感知的等待。
DEFAULT_REDIS_TIMEOUT_SECONDS = 3.0
MAX_REDIS_TIMEOUT_SECONDS = 60.0


def _read_timeout(name: str) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return DEFAULT_REDIS_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REDIS_TIMEOUT_SECONDS
    if not 0 < value <= MAX_REDIS_TIMEOUT_SECONDS:
        return DEFAULT_REDIS_TIMEOUT_SECONDS
    return value


def redis_socket_timeouts() -> tuple[float, float]:
    """连接超时与读写超时（秒），可用 REDIS_SOCKET_CONNECT_TIMEOUT / REDIS_SOCKET_TIMEOUT 覆盖。"""
    return _read_timeout("REDIS_SOCKET_CONNECT_TIMEOUT"), _read_timeout("REDIS_SOCKET_TIMEOUT")


def create_redis_client_from_env(redis_module: Any = None, **kwargs: Any) -> Any:
    """Create a Redis client without concatenating credentials into the URL."""
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None

    try:
        parsed = urlsplit(redis_url)
    except ValueError as exc:
        raise RedisConfigurationError("REDIS_URL 格式无效") from exc

    if parsed.scheme not in {"redis", "rediss", "unix"}:
        raise RedisConfigurationError("REDIS_URL 仅支持 redis、rediss 或 unix 协议")
    if parsed.scheme in {"redis", "rediss"} and not parsed.hostname:
        raise RedisConfigurationError("REDIS_URL 必须包含 Redis 主机")

    username_value = os.getenv("REDIS_USER")
    password_value = os.getenv("REDIS_PASSWORD")
    username = username_value.strip() if username_value else None
    has_password = password_value is not None and password_value != ""
    has_split_credentials = bool(username) or has_password
    query_keys = {
        key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
    }
    has_url_credentials = (
        parsed.username is not None
        or parsed.password is not None
        or bool(query_keys & {"username", "password"})
    )

    if has_split_credentials and has_url_credentials:
        raise RedisConfigurationError(
            "配置 REDIS_USER 或 REDIS_PASSWORD 时，REDIS_URL 不能再包含认证信息"
        )

    options = dict(kwargs)
    if username:
        options["username"] = username
    if has_password:
        options["password"] = password_value

    if redis_module is None:
        import redis.asyncio as redis_module  # type: ignore

    return redis_module.from_url(redis_url, **options)
