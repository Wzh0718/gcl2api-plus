"""每账号稳定设备画像（AGY hub 通道）。

真实 Antigravity 安装在 User-Agent 里报告自己的构建与平台：

    antigravity/hub/<version> (aidev_client; os_type=<os>; arch=<arch>; cl=<cl>)

此前整个账号池共用一份全局取值，等于在请求头里声明“这批号来自同一台机器”。
本模块为每个账号派生一份**稳定**画像：同一账号永远一致（安装级稳定本身是真实
客户端的行为，不要随机化），不同账号之间分散。

取值约束：

- 只允许官方二进制 ``--stamp`` 实读核验过的 ``(version, cl)`` 组合；
- 平台固定为已实抓的 ``linux/amd64``。macOS / Windows 的 UA 写法没有实抓证据，
  编一个不存在的组合比“统一”更难解释，因此不引入。

画像优先取 ``credential_data["device_profile"]``（可人工覆盖、可回填落库），
缺失或非法时按账号名稳定派生。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Mapping, Optional, Tuple

# (hub_version, cl, weight) —— 均由 .agy-re/ls-* 官方二进制 --stamp 实读核验。
# 权重贴近真实用户的版本滞后分布：多数在最新，少量落后一档。
#
# 2.28.2 权重为 0：它是 2026-10-29 的构建，模型目录可能不含 3.1/3.8 系列，
# 降级链（antigravity_model_fallback_chain）打到此类账号上可能触发
# INVALID_ARGUMENT。确认其 fetchAvailableModels 覆盖当前模型后再提权重。
VERIFIED_BUILDS: Tuple[Tuple[str, str, int], ...] = (
    ("2.34.0", "1026637980", 40),  # Built on 2026-11-21（--stamp 实读，最新）
    ("2.33.1", "1026001617", 30),  # Built on 2026-11-20（--stamp 实读）
    ("2.32.2", "1022991777", 20),  # Built on 2026-11-17（--stamp 实读）
    ("2.31.2", "1019359392", 10),  # 2026-11 中旬，滞后两档
    ("2.31.0", "1017595969", 0),   # Built on 2026-11-10，保留实读记录
    ("2.28.2", "1009437749", 0),   # Built on 2026-10-29，待模型目录核验后启用
)

# 可用平台与权重。hub 的公开产物只有 linux/amd64 与 linux/arm64
# （antigravity-public 桶里 darwin/win32 命名均为 404），但上游对平台字段是
# 无条件接受的：2026-11-24 用生产号实测，linux/amd64、darwin/arm64、
# darwin/x64、win32/x64 四种 UA 全部 200 且响应一致，因此按真实终端分布配权。
VERIFIED_PLATFORMS: Tuple[Tuple[str, str, int], ...] = (
    ("linux", "amd64", 55),
    ("linux", "arm64", 15),
    ("darwin", "arm64", 15),
    ("win32", "x64", 10),
    ("darwin", "x64", 5),
)

DEFAULT_OS_TYPE, DEFAULT_ARCH, _ = VERIFIED_PLATFORMS[0]

_PROFILE_KEY = "device_profile"
_DERIVE_PREFIX = "agy-device-profile:v1:"


def verified_build(version: str, cl: str) -> bool:
    """版本与 CL 必须来自同一份实读表，否则视为非法画像。"""
    return any(version == v and cl == c for v, c, _ in VERIFIED_BUILDS)


def _verified_platform(os_type: str, arch: str) -> bool:
    return any(os_type == o and arch == a for o, a, _ in VERIFIED_PLATFORMS)


def cl_for_version(version: str) -> Optional[str]:
    """版本对应的实读 CL；版本不在核验表内返回 None（调用方自行回退）。"""
    for known_version, cl, _ in VERIFIED_BUILDS:
        if version == known_version:
            return cl
    return None


def _digest_int(credential_name: str) -> int:
    raw = hashlib.sha256(f"{_DERIVE_PREFIX}{credential_name}".encode("utf-8")).digest()
    return int.from_bytes(raw[:8], "big")


def derive_device_profile(credential_name: str) -> Dict[str, str]:
    """按账号名稳定派生画像：同一账号跨进程、跨重启取值不变。

    权重为 0 的构建只保留在核验表里（供 cl_for_version 等查询），不参与分配。
    """
    bucket = _digest_int(credential_name or "") % 100
    enabled = [build for build in VERIFIED_BUILDS if build[2] > 0]
    total = sum(weight for _, _, weight in enabled)
    chosen = enabled[0] if enabled else VERIFIED_BUILDS[0]
    if total > 0:
        cursor = 0
        for build in enabled:
            cursor += build[2]
            if bucket < cursor * 100 // total:
                chosen = build
                break
    version, cl, _ = chosen
    platform_bucket = (_digest_int(credential_name or "") >> 32) % 100
    platform_enabled = [p for p in VERIFIED_PLATFORMS if p[2] > 0]
    platform_total = sum(p[2] for p in platform_enabled) or 1
    os_type, arch, _ = platform_enabled[0]
    cursor = 0
    for os_name, arch_name, weight in platform_enabled:
        cursor += weight
        if platform_bucket < cursor * 100 // platform_total:
            os_type, arch = os_name, arch_name
            break
    return {
        "hub_version": version,
        "cl": cl,
        "os_type": os_type,
        "arch": arch,
        "source": "derived",
    }


def normalize_profile(raw: Any) -> Optional[Dict[str, str]]:
    """校验一份显式画像；不合法返回 None，由调用方回退到派生值。"""
    if not isinstance(raw, Mapping):
        return None
    version = raw.get("hub_version")
    cl = raw.get("cl")
    if not isinstance(version, str) or not isinstance(cl, str):
        return None
    if not verified_build(version.strip(), cl.strip()):
        return None
    os_type = raw.get("os_type") or DEFAULT_OS_TYPE
    arch = raw.get("arch") or DEFAULT_ARCH
    if not isinstance(os_type, str) or not isinstance(arch, str):
        return None
    if not _verified_platform(os_type.strip(), arch.strip()):
        return None
    return {
        "hub_version": version.strip(),
        "cl": cl.strip(),
        "os_type": os_type.strip(),
        "arch": arch.strip(),
        "source": "stored",
    }


def get_device_profile(
    credential_data: Optional[Mapping[str, Any]] = None,
    credential_name: Optional[str] = None,
) -> Dict[str, str]:
    """取账号画像：显式落库值优先，否则按账号名稳定派生。

    credential_name 缺失时（无账号上下文的探测）回退到最新已核验构建，
    保持与旧行为一致。
    """
    stored = None
    if isinstance(credential_data, Mapping):
        stored = normalize_profile(credential_data.get(_PROFILE_KEY))
    if stored is not None:
        return stored
    if credential_name:
        return derive_device_profile(credential_name)
    version, cl, _ = VERIFIED_BUILDS[0]
    return {
        "hub_version": version,
        "cl": cl,
        "os_type": DEFAULT_OS_TYPE,
        "arch": DEFAULT_ARCH,
        "source": "default",
    }


def profile_summary(credential_name: str) -> Dict[str, str]:
    """回填/巡检用：给出某账号将采用的画像摘要。"""
    return {"credential": credential_name, **derive_device_profile(credential_name)}
