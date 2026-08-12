"""Safe Redis client construction from environment variables."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import parse_qsl, urlsplit


class RedisConfigurationError(ValueError):
    """Raised for invalid or ambiguous Redis environment configuration."""


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
