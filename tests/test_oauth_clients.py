"""OAuth client 注册表（src/oauth_clients.py）的测试"""

import pytest

from src import oauth_clients
from src.oauth_clients import (
    BUILTIN_CLIENT_KEY,
    get_active_key,
    get_candidates,
    is_client_mismatch_error,
    list_clients,
    reset_oauth_clients,
    set_active_key,
)
from src.utils import ANTIGRAVITY_CLIENT_ID, ANTIGRAVITY_CLIENT_SECRET


@pytest.fixture(autouse=True)
def reset_registry(monkeypatch):
    """每个测试前清空相关环境变量并重置注册表"""
    monkeypatch.delenv("ANTIGRAVITY_OAUTH_CLIENTS", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_OAUTH_CLIENT_KEY", raising=False)
    reset_oauth_clients()
    yield
    reset_oauth_clients()


def test_builtin_default():
    clients = list_clients()
    assert len(clients) == 1
    client = clients[0]
    assert client.key == BUILTIN_CLIENT_KEY
    assert client.client_id == ANTIGRAVITY_CLIENT_ID
    assert client.client_secret == ANTIGRAVITY_CLIENT_SECRET
    assert client.is_builtin is True
    assert get_active_key() == BUILTIN_CLIENT_KEY


def test_env_clients_append(monkeypatch):
    monkeypatch.setenv(
        "ANTIGRAVITY_OAUTH_CLIENTS",
        "Custom_Key|cid-1|csec-1|My Label;other|cid-2|csec-2",
    )
    reset_oauth_clients()

    clients = list_clients()
    assert [c.key for c in clients] == [BUILTIN_CLIENT_KEY, "custom_key", "other"]
    # key 统一小写，label 可省略（缺省回退为 key）
    assert clients[1].label == "My Label"
    assert clients[2].label == "other"
    assert clients[1].is_builtin is False
    # active 不受影响
    assert get_active_key() == BUILTIN_CLIENT_KEY


def test_env_clients_override_builtin(monkeypatch):
    monkeypatch.setenv(
        "ANTIGRAVITY_OAUTH_CLIENTS",
        "antigravity_enterprise|new-id|new-secret",
    )
    reset_oauth_clients()

    clients = list_clients()
    assert len(clients) == 1
    assert clients[0].client_id == "new-id"
    assert clients[0].client_secret == "new-secret"


def test_incomplete_env_entry_skipped(monkeypatch):
    monkeypatch.setenv(
        "ANTIGRAVITY_OAUTH_CLIENTS",
        "bad|only-two-parts;|missing-secret||;good|gid|gsec",
    )
    reset_oauth_clients()

    keys = [c.key for c in list_clients()]
    assert keys == [BUILTIN_CLIENT_KEY, "good"]


def test_active_key_from_env(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENTS", "foo|fid|fsec")
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENT_KEY", "FOO")
    reset_oauth_clients()
    assert get_active_key() == "foo"


def test_active_key_fallback_when_missing(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENTS", "foo|fid|fsec")
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENT_KEY", "not-exist")
    reset_oauth_clients()
    # 指定的 key 不存在时回退到列表第一个
    assert get_active_key() == BUILTIN_CLIENT_KEY


def test_set_active_key(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENTS", "foo|fid|fsec")
    reset_oauth_clients()

    assert set_active_key("foo") is True
    assert get_active_key() == "foo"
    assert set_active_key("not-exist") is False
    assert get_active_key() == "foo"


def test_candidates_order_and_dedup(monkeypatch):
    monkeypatch.setenv(
        "ANTIGRAVITY_OAUTH_CLIENTS",
        "foo|fid|fsec;bar|bid|bsec",
    )
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENT_KEY", "foo")
    reset_oauth_clients()

    # preferred → active → 其余，去重
    candidates = get_candidates("bar")
    assert [c.key for c in candidates] == ["bar", "foo", BUILTIN_CLIENT_KEY]

    # preferred 与 active 相同时不重复
    candidates = get_candidates("foo")
    assert [c.key for c in candidates] == ["foo", BUILTIN_CLIENT_KEY, "bar"]

    # preferred 不存在时忽略
    candidates = get_candidates("not-exist")
    assert [c.key for c in candidates] == ["foo", BUILTIN_CLIENT_KEY, "bar"]

    # 无 preferred：active 优先
    candidates = get_candidates()
    assert [c.key for c in candidates] == ["foo", BUILTIN_CLIENT_KEY, "bar"]


@pytest.mark.parametrize(
    "status_code,error_text,expected",
    [
        (400, "", True),
        (401, "", True),
        (403, "", True),
        (500, "", False),
        (None, "", False),
        (None, "unauthorized_client", True),
        (None, '{"error": "invalid_client"}', True),
        (None, "INVALID_CLIENT", True),
        (None, "connection timeout", False),
        (500, "internal server error", False),
    ],
)
def test_is_client_mismatch_error(status_code, error_text, expected):
    assert is_client_mismatch_error(status_code, error_text) is expected
