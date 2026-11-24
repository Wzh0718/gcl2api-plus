"""P0-1 / P0-2：账号设备画像与静默窗口的回归测试。

约束（改动前先读）：
- 同一账号的画像与 UA 必须跨进程稳定 —— 安装级稳定是真实客户端的行为；
- 跨账号必须分散 —— 全池同一份指纹本身就是最明显的“同一台机器”信号；
- 只允许使用官方二进制 --stamp 实读核验过的 (version, cl) 组合。
"""

import re
from datetime import datetime

import pytest

from src.antigravity_device_profile import (
    VERIFIED_BUILDS,
    derive_device_profile,
    get_device_profile,
    normalize_profile,
)
from src.utils import build_antigravity_user_agent, get_antigravity_user_agent_for

HUB_UA_RE = re.compile(
    r"^antigravity/hub/(?P<version>\d+\.\d+\.\d+) "
    r"\(aidev_client; os_type=(?P<os>[a-z0-9]+); arch=(?P<arch>[a-z0-9]+); cl=(?P<cl>\d+)\)$"
)

CREDENTIALS = [
    "ag_aicode-consumers-1793852596.json",
    "ag_aicode-consumers-1793852616.json",
    "ag_aicode-consumers-1793852636.json",
    "ag_aicode-consumers-1793853968.json",
    "ag_aicode-consumers-1793853982.json",
    "ag_aicode-consumers-1793853992.json",
    "ag_aicode-consumers-1793854002.json",
    "ag_aicode-consumers-1793854015.json",
    "ag_aicode-consumers-1795078922.json",
    "ag_aicode-consumers-1795078936.json",
    "ag_aicode-consumers-1795078908.json",
    "ag_aicode-consumers-1795137738.json",
    "ag_aicode-consumers-1795137772.json",
    "ag_aicode-consumers-1795137724.json",
]


def test_derived_profile_is_stable_for_the_same_account():
    for name in CREDENTIALS:
        assert derive_device_profile(name) == derive_device_profile(name)


def test_derived_profile_only_uses_verified_builds():
    verified = {(v, c) for v, c, _ in VERIFIED_BUILDS}
    for name in CREDENTIALS:
        profile = derive_device_profile(name)
        assert (profile["hub_version"], profile["cl"]) in verified


def test_accounts_do_not_share_one_global_build():
    versions = {derive_device_profile(name)["hub_version"] for name in CREDENTIALS}
    assert len(versions) >= 2, "池内仍在使用单一构建指纹"


def test_stored_profile_is_honoured():
    stored = {
        "device_profile": {
            "hub_version": "2.28.2",
            "cl": "1009437749",
            "os_type": "linux",
            "arch": "amd64",
        }
    }
    profile = get_device_profile(stored, "any.json")
    assert profile["source"] == "stored"
    assert profile["hub_version"] == "2.28.2"


def test_unverified_stored_profile_falls_back_to_derived():
    bad = {"device_profile": {"hub_version": "9.9.9", "cl": "1234567890"}}
    profile = get_device_profile(bad, CREDENTIALS[0])
    assert profile["source"] == "derived"
    assert profile == derive_device_profile(CREDENTIALS[0])


def test_version_and_cl_must_come_from_the_same_row():
    mismatch = {"device_profile": {"hub_version": "2.31.2", "cl": "1009437749"}}
    assert normalize_profile(mismatch["device_profile"]) is None


def test_unverified_platform_is_rejected():
    odd = {
        "device_profile": {
            "hub_version": "2.31.2",
            "cl": "1019359392",
            "os_type": "freebsd",
            "arch": "riscv64",
        }
    }
    assert normalize_profile(odd["device_profile"]) is None


def test_mac_and_windows_platforms_are_accepted():
    # 2026-11-24 生产号实测：darwin/arm64、darwin/x64、win32/x64 三种 UA 均返回 200，
    # 响应与 linux/amd64 一致，因此这三组进入 VERIFIED_PLATFORMS。
    for os_type, arch in (("darwin", "arm64"), ("darwin", "x64"), ("win32", "x64")):
        profile = normalize_profile({
            "hub_version": "2.34.0",
            "cl": "1026637980",
            "os_type": os_type,
            "arch": arch,
        })
        assert profile is not None
        assert (profile["os_type"], profile["arch"]) == (os_type, arch)


def test_per_account_user_agent_is_well_formed_and_self_consistent():
    cl_by_version = {v: c for v, c, _ in VERIFIED_BUILDS}
    for name in CREDENTIALS:
        ua = get_antigravity_user_agent_for(None, name)
        match = HUB_UA_RE.match(ua)
        assert match, ua
        assert match.group("cl") == cl_by_version[match.group("version")]


def test_user_agent_is_stable_per_account_and_differs_between_accounts():
    first = [get_antigravity_user_agent_for(None, name) for name in CREDENTIALS]
    second = [get_antigravity_user_agent_for(None, name) for name in CREDENTIALS]
    assert first == second
    assert len(set(first)) >= 2


def test_no_account_context_keeps_global_configuration():
    # 无账号上下文的探测调用必须维持原行为，不引入画像。
    assert get_antigravity_user_agent_for() == build_antigravity_user_agent()
    assert get_antigravity_user_agent_for(None, None) == build_antigravity_user_agent()


class TestDormantWindow:
    def test_disabled_by_default(self, monkeypatch):
        import src.antigravity_pacing as pacing

        monkeypatch.delenv("ANTIGRAVITY_DORMANT_ENABLED", raising=False)
        assert not any(
            pacing.is_dormant(name, now=datetime(2026, 1, 1, hour, 0))
            for name in CREDENTIALS
            for hour in range(24)
        )

    def test_window_covers_exact_hours_and_is_contiguous(self, monkeypatch):
        import src.antigravity_pacing as pacing

        monkeypatch.setenv("ANTIGRAVITY_DORMANT_ENABLED", "true")
        monkeypatch.setenv("ANTIGRAVITY_DORMANT_HOURS", "6")
        for name in CREDENTIALS[:4]:
            bits = [
                1 if pacing.is_dormant(name, now=datetime(2026, 1, 1, hour, 0)) else 0
                for hour in range(24)
            ]
            assert sum(bits) == 6
            start = pacing.dormant_window_start_hour(name)
            expected = [1 if (hour - start) % 24 < 6 else 0 for hour in range(24)]
            assert bits == expected

    def test_start_hours_spread_across_the_clock(self, monkeypatch):
        import src.antigravity_pacing as pacing

        hours = {pacing.dormant_window_start_hour(f"acct-{i}.json") for i in range(500)}
        assert len(hours) > 18, "静默窗口没有散开，仍会形成整池同时离线"

    def test_pool_never_has_a_capacity_cliff(self, monkeypatch):
        import src.antigravity_pacing as pacing

        monkeypatch.setenv("ANTIGRAVITY_DORMANT_ENABLED", "true")
        monkeypatch.setenv("ANTIGRAVITY_DORMANT_HOURS", "6")
        names = [f"acct-{i}.json" for i in range(2000)]
        availability = [
            sum(
                1
                for name in names
                if not pacing.is_dormant(name, now=datetime(2026, 1, 1, hour, 0))
            )
            / len(names)
            for hour in range(24)
        ]
        # 6/24 静默 → 任一时刻约有 75% 可用，且不出现某个小时塌到很低。
        assert min(availability) > 0.70
        assert max(availability) - min(availability) < 0.05

    def test_dormant_exception_is_routable(self):
        import src.antigravity_pacing as pacing

        exc = pacing.CredentialDormant("acct.json")
        assert isinstance(exc, pacing.CredentialSlotWaitTimeout)
        assert exc.rate_limit.reason == "dormant_window"


@pytest.mark.parametrize("hours", ["0", "20", "99", "abc"])
def test_dormant_hours_are_clamped(monkeypatch, hours):
    import src.antigravity_pacing as pacing

    monkeypatch.setenv("ANTIGRAVITY_DORMANT_ENABLED", "true")
    monkeypatch.setenv("ANTIGRAVITY_DORMANT_HOURS", hours)
    name = CREDENTIALS[0]
    dormant_hours = sum(
        1 for hour in range(24) if pacing.is_dormant(name, now=datetime(2026, 1, 1, hour, 0))
    )
    assert 0 <= dormant_hours <= 20
