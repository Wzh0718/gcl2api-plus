"""pytest 全局夹具。

2026-10-21：Antigravity 账号请求节奏（pacing）默认开启会让全量测试慢一个数量级
（每个伪造账号的上游尝试都要等 1.5s 基准间隔），并破坏基于严格时序的取消/释放断言。
测试环境默认关闭 pacing；专项测试（tests/test_antigravity_pacing_quota.py）
用自己的夹具显式打开。
"""

import pytest


@pytest.fixture(autouse=True)
def _disable_antigravity_pacing(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_PACING_ENABLED", "false")
    yield
