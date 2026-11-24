"""模型级容量耗尽（503 MODEL_CAPACITY_EXHAUSTED）不得冷藏账号。

生产实测（2026-11-24 10:34，gemini-2.5-flash-lite）：上游 503 的响应体带
MODEL_CAPACITY_EXHAUSTED 与 RetryInfo.retryDelay=40s，同一分钟内 13 个不同
账号收到完全相同的响应。旧代码对任意 503 一律 hold 43200s，一次上游容量
抖动就把整池账号关了半天。
"""

import pytest

from src.antigravity_error_classifier import (
    MODEL_CAPACITY_MARKER,
    is_model_capacity_exhausted,
)

# 与线上抓到的响应体一致（仅缩进压缩）
CAPACITY_BODY = (
    '{"error": {"code": 503, "message": "No capacity available for model '
    'gemini-2.5-flash-lite on the server", "status": "UNAVAILABLE", '
    '"details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", '
    '"reason": "MODEL_CAPACITY_EXHAUSTED", '
    '"domain": "cloudcode-pa.googleapis.com", '
    '"metadata": {"error_number": "2010", "model": "gemini-2.5-flash-lite"}}, '
    '{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "38s"}]}}'
)

# 账号级 503：没有容量标记
ACCOUNT_BODY = (
    '{"error": {"code": 503, "message": "The service is currently unavailable", '
    '"status": "UNAVAILABLE"}}'
)


def test_marker_matches_production_body():
    assert MODEL_CAPACITY_MARKER == "MODEL_CAPACITY_EXHAUSTED"
    assert is_model_capacity_exhausted(503, CAPACITY_BODY) is True


@pytest.mark.parametrize(
    "status,body",
    [
        (503, ACCOUNT_BODY),
        (503, ""),
        (503, None),
        (500, CAPACITY_BODY),
        (429, CAPACITY_BODY),
        (200, CAPACITY_BODY),
        (None, CAPACITY_BODY),
    ],
)
def test_non_capacity_cases_are_not_exempted(status, body):
    assert is_model_capacity_exhausted(status, body) is False


@pytest.mark.asyncio
async def test_retry_helper_does_not_hold_account_on_capacity(monkeypatch):
    """容量型 503：仍然换号重试，但不把账号放进 43200s 冷却。"""
    from src.api import utils

    held = []

    async def fake_hold(credential_manager, status_code, credential_name):
        held.append((status_code, credential_name))

    monkeypatch.setattr(utils, "hold_transient_account", fake_hold)
    monkeypatch.setattr(utils.asyncio, "sleep", lambda *_: _noop())

    should_retry = await utils.handle_error_with_retry(
        object(), 503, "a.json", True, 0, 3, 0.0,
        mode="antigravity", error_text=CAPACITY_BODY,
    )

    assert should_retry is True
    assert held == [], "模型级容量耗尽不应冷藏账号"


@pytest.mark.asyncio
async def test_retry_helper_still_holds_on_account_level_503(monkeypatch):
    from src.api import utils

    held = []

    async def fake_hold(credential_manager, status_code, credential_name):
        held.append((status_code, credential_name))

    monkeypatch.setattr(utils, "hold_transient_account", fake_hold)
    monkeypatch.setattr(utils.asyncio, "sleep", lambda *_: _noop())

    should_retry = await utils.handle_error_with_retry(
        object(), 503, "a.json", True, 0, 3, 0.0,
        mode="antigravity", error_text=ACCOUNT_BODY,
    )

    assert should_retry is True
    assert held == [(503, "a.json")]


@pytest.mark.asyncio
async def test_image_capacity_guard_uses_same_semantics():
    """图片路径的容量判定与文本共用同一语义（不再只看 image 模型）。"""
    from src.api import antigravity as agy

    assert agy.is_image_model_capacity_exhausted(503, CAPACITY_BODY) is True
    assert agy.is_image_model_capacity_exhausted(503, ACCOUNT_BODY) is False


async def _noop():
    return None
