"""Configuration and derived budgets for Antigravity dynamic warmup."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping


DEFAULT_WARMUP_CONFIG: dict[str, Any] = {
    "account_initial_concurrency": 1,
    "account_target_concurrency": 0,
    "account_initial_interval_ms": 1500,
    "account_target_interval_ms": 80,
    "ip_initial_concurrency": 1,
    "ip_target_concurrency": 1,
    "ip_initial_interval_ms": 1500,
    "ip_target_interval_ms": 80,
    "success_threshold": 10,
    "window_seconds": 1800,
    "max_level": 3,
    "lease_ttl_seconds": 300,
}

_NON_SELECTABLE_STATUSES = {"paused", "cancelled", "account_forbidden"}


def warmup_selectable(status: str) -> bool:
    return status not in _NON_SELECTABLE_STATUSES


def normalize_warmup_config(
    config: Mapping[str, Any] | None = None,
    *,
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = dict(DEFAULT_WARMUP_CONFIG)
    if base:
        normalized.update(base)
    if config:
        normalized.update(config)

    for scope in ("account", "ip"):
        initial_key = f"{scope}_initial_concurrency"
        target_key = f"{scope}_target_concurrency"
        initial_interval_key = f"{scope}_initial_interval_ms"
        target_interval_key = f"{scope}_target_interval_ms"
        try:
            initial = int(normalized[initial_key])
            target = int(normalized[target_key])
            initial_interval = int(normalized[initial_interval_key])
            target_interval = int(normalized[target_interval_key])
        except (TypeError, ValueError) as exc:
            raise ValueError("养号并发和间隔参数必须是整数") from exc
        maximum_target = 64 if scope == "account" else 10000
        if not 1 <= initial <= maximum_target or not 0 <= target <= maximum_target:
            raise ValueError(f"{scope} 养号并发参数超出范围")
        if target and target < initial:
            raise ValueError(f"{scope} 目标并发不得小于初始并发")
        if not 0 <= initial_interval <= 600000 or not 0 <= target_interval <= 600000:
            raise ValueError(f"{scope} 发起间隔必须在 0-600000ms 之间")
        if target_interval > initial_interval:
            raise ValueError(f"{scope} 目标发起间隔不得大于初始间隔")
        normalized[initial_key] = initial
        normalized[target_key] = target
        normalized[initial_interval_key] = initial_interval
        normalized[target_interval_key] = target_interval

    for key, minimum, maximum in (
        ("success_threshold", 1, 10000),
        ("window_seconds", 0, 604800),
        ("max_level", 1, 10),
        ("lease_ttl_seconds", 30, 86400),
    ):
        try:
            value = int(normalized[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是整数") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum}-{maximum} 之间")
        normalized[key] = value
    return normalized


def warmup_limits(config: Mapping[str, Any], level: int) -> dict[str, Any]:
    max_level = int(config["max_level"])
    level = min(max(int(level), 0), max_level)
    ratio = level / max_level
    from config import get_antigravity_pacing_concurrency_sync

    account_target = int(config["account_target_concurrency"])
    if account_target == 0:
        account_target = max(
            get_antigravity_pacing_concurrency_sync(),
            int(config["account_initial_concurrency"]),
        )

    def ascending(initial: int, target: int) -> int:
        return round(initial + (target - initial) * ratio)

    def descending(initial: int, target: int) -> int:
        return round(initial - (initial - target) * ratio)

    return {
        "account_concurrency": max(
            ascending(int(config["account_initial_concurrency"]), account_target), 1
        ),
        "account_interval": max(
            descending(
                int(config["account_initial_interval_ms"]),
                int(config["account_target_interval_ms"]),
            )
            / 1000,
            0,
        ),
        "ip_concurrency": max(
            ascending(
                int(config["ip_initial_concurrency"]),
                int(config["ip_target_concurrency"]),
            ),
            1,
        ),
        "ip_interval": max(
            descending(
                int(config["ip_initial_interval_ms"]),
                int(config["ip_target_interval_ms"]),
            )
            / 1000,
            0,
        ),
        "success_threshold": int(config["success_threshold"]),
        "window_seconds": int(config["window_seconds"]),
        "max_level": max_level,
        "lease_ttl_seconds": int(config["lease_ttl_seconds"]),
    }


def warmup_ip_key(network: Mapping[str, Any], health: Mapping[str, Any]) -> str:
    egress_ip = str(health.get("egress_ip") or "").strip()
    if egress_ip:
        return f"ip:{egress_ip}"
    node_id = network.get("bound_proxy_node_id")
    if node_id:
        return f"node:{int(node_id)}"
    proxy_url = str(network.get("bound_proxy_url") or network.get("proxy_url") or "")
    if proxy_url:
        digest = hashlib.sha256(proxy_url.encode("utf-8")).hexdigest()[:24]
        return f"proxy:{digest}"
    mode = str(network.get("proxy_mode") or "inherit")
    return f"mode:{mode}"
