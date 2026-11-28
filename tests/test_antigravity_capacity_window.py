"""模型级容量窗口的登记、收敛与过期。

窗口只影响「下一个请求要不要直接改投降级链」，不拒绝任何请求；这里的断言都
围绕这一点：默认窗口、上限/下限收敛、重复登记取更晚、过期即失效。
"""

import pytest

import src.antigravity_capacity_window as window


NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _clean():
    window.reset_capacity_windows()
    yield
    window.reset_capacity_windows()


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(window.time, "monotonic", lambda: NOW)


@pytest.mark.parametrize(
    "delay,expected",
    [
        (None, window.DEFAULT_WINDOW_SECONDS),
        (38.0, 38.0),
        (0, window.DEFAULT_WINDOW_SECONDS),
        (-5, window.DEFAULT_WINDOW_SECONDS),
        (1, window.MIN_WINDOW_SECONDS),
        (10_000, window.MAX_WINDOW_SECONDS),
        ("abc", window.DEFAULT_WINDOW_SECONDS),
    ],
)
def test_window_seconds_are_clamped(delay, expected):
    assert window.window_seconds(delay) == expected


def test_default_window_follows_env(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_CAPACITY_WINDOW_SECONDS", "12")
    assert window.window_seconds(None) == 12.0
    monkeypatch.setenv("ANTIGRAVITY_CAPACITY_WINDOW_SECONDS", "0")
    assert window.window_seconds(None) == window.DEFAULT_WINDOW_SECONDS


def test_record_and_expire(frozen):
    assert window.capacity_window_remaining("m") == 0
    window.record_capacity_window("m", 38)
    assert window.capacity_window_remaining("m") == pytest.approx(38)


def test_repeated_record_keeps_later_deadline(frozen):
    window.record_capacity_window("m", 60)
    window.record_capacity_window("m", 10)  # 更短的尾巴不能把窗口缩短
    assert window.capacity_window_remaining("m") == pytest.approx(60)


def test_windows_are_per_model(frozen):
    window.record_capacity_window("a", 30)
    assert window.capacity_window_remaining("b") == 0
    assert window.capacity_window_snapshot() == {"a": pytest.approx(30)}


def test_expired_window_is_dropped(monkeypatch, frozen):
    window.record_capacity_window("m", 5)
    monkeypatch.setattr(window.time, "monotonic", lambda: NOW + 5.5)
    assert window.capacity_window_remaining("m") == 0
    assert window.capacity_window_snapshot() == {}


def test_blank_model_is_ignored(frozen):
    assert window.record_capacity_window("", 30) == 0
    assert window.record_capacity_window("   ", 30) == 0
    assert window.capacity_window_snapshot() == {}
