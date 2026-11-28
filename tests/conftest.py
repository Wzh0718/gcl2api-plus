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


@pytest.fixture(autouse=True)
def _sqlite_test_backend(monkeypatch):
    """隔离宿主机的主库配置，使用默认 SQLite。"""
    monkeypatch.delenv("MAIN_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("GCLI_COMPOSE_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("MAIN_MYSQL_DSN", raising=False)
    monkeypatch.delenv("MAIN_POSTGRES_DSN", raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_capacity_windows():
    """模型级容量窗口是进程内全局状态：每个用例前后清空，避免顺序影响断言。"""
    from src.antigravity_capacity_window import reset_capacity_windows

    reset_capacity_windows()
    yield
    reset_capacity_windows()
