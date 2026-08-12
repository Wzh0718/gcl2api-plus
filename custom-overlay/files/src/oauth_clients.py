"""
Antigravity OAuth client 注册表

语义对齐 Antigravity-Manager 的 modules/oauth.rs：
- 内置 antigravity_enterprise client（复用 src/utils.py 中的常量）
- 环境变量 ANTIGRAVITY_OAUTH_CLIENTS 追加/覆盖 client
  （格式: key|client_id|client_secret|label;key2|id2|secret2，label 可省略）
- 环境变量 ANTIGRAVITY_OAUTH_CLIENT_KEY 指定 active key
"""

import os
from dataclasses import dataclass
from typing import List, Optional

from log import log

from src.utils import ANTIGRAVITY_CLIENT_ID, ANTIGRAVITY_CLIENT_SECRET

# 内置 client 的 key 与环境变量名
BUILTIN_CLIENT_KEY = "antigravity_enterprise"
ENV_OAUTH_CLIENTS = "ANTIGRAVITY_OAUTH_CLIENTS"
ENV_OAUTH_CLIENT_KEY = "ANTIGRAVITY_OAUTH_CLIENT_KEY"


@dataclass
class OAuthClientConfig:
    """OAuth client 配置"""

    key: str
    label: str
    client_id: str
    client_secret: str
    is_builtin: bool = False


# 惰性初始化的注册表状态（reset_oauth_clients 可重置，便于测试）
_clients: Optional[List[OAuthClientConfig]] = None
_active_key: Optional[str] = None


def _builtin_client() -> OAuthClientConfig:
    return OAuthClientConfig(
        key=BUILTIN_CLIENT_KEY,
        label="Antigravity Enterprise",
        client_id=ANTIGRAVITY_CLIENT_ID,
        client_secret=ANTIGRAVITY_CLIENT_SECRET,
        is_builtin=True,
    )


def _parse_env_clients() -> List[OAuthClientConfig]:
    """解析 ANTIGRAVITY_OAUTH_CLIENTS 环境变量，不完整的条目跳过并告警"""
    raw = os.getenv(ENV_OAUTH_CLIENTS, "").strip()
    if not raw:
        return []

    clients = []
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = [part.strip() for part in entry.split("|")]
        if len(parts) < 3 or not all(parts[:3]):
            log.warning(f"跳过不完整的 OAuth client 配置: {entry}")
            continue
        key = parts[0].lower()
        label = parts[3] if len(parts) > 3 and parts[3] else key
        clients.append(
            OAuthClientConfig(
                key=key,
                label=label,
                client_id=parts[1],
                client_secret=parts[2],
                is_builtin=False,
            )
        )
    return clients


def _ensure_initialized():
    """惰性初始化注册表：内置 client + 环境变量追加/覆盖"""
    global _clients, _active_key
    if _clients is not None:
        return

    clients = [_builtin_client()]
    for env_client in _parse_env_clients():
        # 同 key 覆盖（可覆盖内置 client），否则追加
        for i, existing in enumerate(clients):
            if existing.key == env_client.key:
                clients[i] = env_client
                break
        else:
            clients.append(env_client)
    _clients = clients

    # active key：环境变量指定，缺省内置 key；指定不存在则回退到列表第一个
    requested = os.getenv(ENV_OAUTH_CLIENT_KEY, "").strip().lower()
    if requested and any(c.key == requested for c in _clients):
        _active_key = requested
    elif requested:
        log.warning(f"指定的 OAuth client key 不存在: {requested}，回退到 {_clients[0].key}")
        _active_key = _clients[0].key
    else:
        _active_key = BUILTIN_CLIENT_KEY if any(
            c.key == BUILTIN_CLIENT_KEY for c in _clients
        ) else _clients[0].key


def reset_oauth_clients():
    """重置注册表（测试用，修改环境变量后调用）"""
    global _clients, _active_key
    _clients = None
    _active_key = None


def list_clients() -> List[OAuthClientConfig]:
    """列出全部 OAuth client"""
    _ensure_initialized()
    return list(_clients)


def get_active_key() -> str:
    """获取当前 active client 的 key"""
    _ensure_initialized()
    return _active_key


def set_active_key(key: str) -> bool:
    """设置 active client，key 不存在时返回 False"""
    global _active_key
    _ensure_initialized()
    key = key.lower()
    if not any(c.key == key for c in _clients):
        log.warning(f"尝试设置不存在的 OAuth client key: {key}")
        return False
    _active_key = key
    return True


def get_candidates(preferred_key: Optional[str] = None) -> List[OAuthClientConfig]:
    """
    获取候选 client 顺序：preferred_key → active → 其余全部，去重
    """
    _ensure_initialized()
    ordered_keys = []
    if preferred_key:
        ordered_keys.append(preferred_key.lower())
    ordered_keys.append(_active_key)
    ordered_keys.extend(c.key for c in _clients)

    seen = set()
    candidates = []
    by_key = {c.key: c for c in _clients}
    for key in ordered_keys:
        if key in seen or key not in by_key:
            continue
        seen.add(key)
        candidates.append(by_key[key])
    return candidates


def is_client_mismatch_error(status_code: Optional[int], error_text: str) -> bool:
    """
    判断错误是否为 "client 不匹配"（只有此类错误才降级到下一个 client）
    规则：HTTP 状态码为 400/401/403，或错误文本包含 unauthorized_client / invalid_client
    """
    if status_code in (400, 401, 403):
        return True
    text = (error_text or "").lower()
    return "unauthorized_client" in text or "invalid_client" in text
