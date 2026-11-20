import pytest


class FakeRedisModule:
    def __init__(self):
        self.calls = []

    def from_url(self, url, **kwargs):
        client = {"url": url, "kwargs": kwargs}
        self.calls.append(client)
        return client


def test_split_redis_credentials_are_passed_without_url_encoding(monkeypatch):
    from src.redis_config import create_redis_client_from_env

    monkeypatch.setenv("REDIS_URL", "redis://redis.example:6379/3")
    monkeypatch.setenv("REDIS_USER", " default ")
    monkeypatch.setenv("REDIS_PASSWORD", "#p@ss:word")
    redis_module = FakeRedisModule()

    client = create_redis_client_from_env(
        redis_module,
        decode_responses=True,
        socket_connect_timeout=1,
    )

    assert client == {
        "url": "redis://redis.example:6379/3",
        "kwargs": {
            "decode_responses": True,
            "socket_connect_timeout": 1,
            "username": "default",
            "password": "#p@ss:word",
        },
    }


def test_legacy_redis_url_credentials_remain_supported(monkeypatch):
    from src.redis_config import create_redis_client_from_env

    monkeypatch.setenv(
        "REDIS_URL", "redis://legacy:%23encoded@redis.example:6379/0"
    )
    monkeypatch.delenv("REDIS_USER", raising=False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    redis_module = FakeRedisModule()

    client = create_redis_client_from_env(redis_module, decode_responses=True)

    assert client == {
        "url": "redis://legacy:%23encoded@redis.example:6379/0",
        "kwargs": {"decode_responses": True},
    }


def test_split_and_embedded_redis_credentials_cannot_be_mixed(monkeypatch):
    from src.redis_config import RedisConfigurationError, create_redis_client_from_env

    monkeypatch.setenv(
        "REDIS_URL", "redis://legacy:encoded@redis.example:6379/0"
    )
    monkeypatch.setenv("REDIS_PASSWORD", "separate-secret")
    redis_module = FakeRedisModule()

    with pytest.raises(RedisConfigurationError) as exc_info:
        create_redis_client_from_env(redis_module)

    assert "separate-secret" not in str(exc_info.value)
    assert "redis.example" not in str(exc_info.value)
    assert redis_module.calls == []


def test_split_and_query_string_redis_credentials_cannot_be_mixed(monkeypatch):
    from src.redis_config import RedisConfigurationError, create_redis_client_from_env

    monkeypatch.setenv(
        "REDIS_URL", "redis://redis.example:6379/0?password=legacy-secret"
    )
    monkeypatch.setenv("REDIS_PASSWORD", "separate-secret")
    redis_module = FakeRedisModule()

    with pytest.raises(RedisConfigurationError) as exc_info:
        create_redis_client_from_env(redis_module)

    assert "legacy-secret" not in str(exc_info.value)
    assert "separate-secret" not in str(exc_info.value)
    assert "redis.example" not in str(exc_info.value)
    assert redis_module.calls == []


def test_malformed_legacy_redis_url_does_not_expose_its_secret(monkeypatch):
    from src.redis_config import RedisConfigurationError, create_redis_client_from_env

    monkeypatch.setenv(
        "REDIS_URL", "redis://:#private-secret@redis.example:6379/3"
    )
    monkeypatch.delenv("REDIS_USER", raising=False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)

    with pytest.raises(RedisConfigurationError) as exc_info:
        create_redis_client_from_env(FakeRedisModule())

    assert "private-secret" not in str(exc_info.value)
    assert "redis.example" not in str(exc_info.value)


def test_missing_redis_url_disables_client_without_importing_or_logging_secrets(monkeypatch):
    from src.redis_config import create_redis_client_from_env

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_USER", "unused-user")
    monkeypatch.setenv("REDIS_PASSWORD", "unused-secret")
    redis_module = FakeRedisModule()

    assert create_redis_client_from_env(redis_module) is None
    assert redis_module.calls == []


def test_socket_timeouts_default_to_three_seconds(monkeypatch):
    from src.redis_config import DEFAULT_REDIS_TIMEOUT_SECONDS, redis_socket_timeouts

    monkeypatch.delenv("REDIS_SOCKET_CONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("REDIS_SOCKET_TIMEOUT", raising=False)

    # 1 秒在高并发下会被 DNS 排队打穿（见 2026-11-20 生产排查），默认给 3 秒。
    assert DEFAULT_REDIS_TIMEOUT_SECONDS >= 3.0
    assert redis_socket_timeouts() == (
        DEFAULT_REDIS_TIMEOUT_SECONDS,
        DEFAULT_REDIS_TIMEOUT_SECONDS,
    )


def test_socket_timeouts_are_env_overridable_and_clamped(monkeypatch):
    from src.redis_config import DEFAULT_REDIS_TIMEOUT_SECONDS, redis_socket_timeouts

    monkeypatch.setenv("REDIS_SOCKET_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", "0.5")
    assert redis_socket_timeouts() == (5.0, 0.5)

    for broken in ("0", "-1", "abc", "600"):
        monkeypatch.setenv("REDIS_SOCKET_CONNECT_TIMEOUT", broken)
        monkeypatch.setenv("REDIS_SOCKET_TIMEOUT", broken)
        assert redis_socket_timeouts() == (
            DEFAULT_REDIS_TIMEOUT_SECONDS,
            DEFAULT_REDIS_TIMEOUT_SECONDS,
        ), broken
