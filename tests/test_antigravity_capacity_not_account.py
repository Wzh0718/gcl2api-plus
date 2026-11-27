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

    should_retry = await utils.handle_error_with_retry(
        object(), 503, "a.json", True, 0, 3,
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

    should_retry = await utils.handle_error_with_retry(
        object(), 503, "a.json", True, 0, 3,
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


# ==================== 503 分类与「一次原号重试」（2026-11-26 线上复盘） ====================
# 复盘口径（60 分钟窗口）：
# - 956 次 503 中 862 次给 flash-lite 写了模型级冷却，均值 42s，锁掉约 23% 池容量；
# - 536 次容量 503 分布在 536 个请求上，没有任何请求撞到第二次，
#   客户端可见 503 只有 3 个 → 一次原号重试足够，第二次之后只放大延迟；
# - 但 503 不能一律重试：配额型要冷却换号，服务级/未知正文按账号级处理。

QUOTA_BODY = (
    '{"error": {"code": 503, "message": "Individual quota reached. Please upgrade '
    'your subscription to increase your limits. Resets in 154h21m14s.", '
    '"status": "RESOURCE_EXHAUSTED", "details": [{"@type": '
    '"type.googleapis.com/google.rpc.ErrorInfo", "reason": "QUOTA_EXHAUSTED", '
    '"metadata": {"quotaResetTimeStamp": "2026-11-27T01:29:45Z"}}]}}'
)
OVERLOADED_BODY = (
    '{"error": {"code": 503, "status": "UNAVAILABLE", '
    '"message": "The model is overloaded. Please try again later."}}'
)
GENERIC_UNAVAILABLE_BODY = (
    '{"error": {"code": 503, "status": "UNAVAILABLE", '
    '"message": "The service is currently unavailable"}}'
)
CAPACITY_BY_ERROR_NUMBER = (
    '{"error": {"code": 503, "message": "upstream reject", "details": '
    '[{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "UNAVAILABLE", '
    '"metadata": {"error_number": "2010"}}]}}'
)
CAPACITY_BY_MESSAGE = (
    '{"error": {"code": 503, "message": '
    '"No capacity available for model gemini-2.5-flash-lite on the server"}}'
)


@pytest.mark.parametrize(
    "body,category,retry,hold,cooldown",
    [
        (CAPACITY_BODY, "model_capacity", True, False, False),
        (CAPACITY_BY_ERROR_NUMBER, "model_capacity", True, False, False),
        (CAPACITY_BY_MESSAGE, "model_capacity", True, False, False),
        (QUOTA_BODY, "quota", False, False, True),
        (OVERLOADED_BODY, "model_overloaded", False, True, True),
        (GENERIC_UNAVAILABLE_BODY, "service_unavailable", False, True, True),
        ('{"error": {"code": 503, "status": "UNAVAILABLE"}}', "service_unavailable", False, True, True),
        ("plain text upstream error", "service_unavailable", False, True, True),
        ("", "unknown", False, True, True),
        (None, "unknown", False, True, True),
        ("{not json", "service_unavailable", False, True, True),  # 拆不开也不当容量
    ],
)
def test_503_classification_drives_actions(body, category, retry, hold, cooldown):
    from src.antigravity_error_classifier import classify_antigravity_503

    decision = classify_antigravity_503(503, body)
    assert decision.category == category
    assert decision.retry_same_account is retry
    assert decision.hold_account is hold
    assert decision.write_cooldown is cooldown


def test_capacity_decision_reports_structured_evidence():
    """分类要能说出它是靠什么判的，排障时不用再猜。"""
    from src.antigravity_error_classifier import classify_antigravity_503

    decision = classify_antigravity_503(503, CAPACITY_BODY)
    assert decision.marker == "reason"
    assert decision.reason == "MODEL_CAPACITY_EXHAUSTED"
    assert decision.upstream_model == "gemini-2.5-flash-lite"
    assert decision.upstream_delay_seconds == pytest.approx(38.0)
    assert "category=model_capacity" in decision.describe()
    assert "retryDelay=38s" in decision.describe()


def test_non_503_is_never_treated_as_capacity():
    from src.antigravity_error_classifier import classify_antigravity_503

    for status in (429, 500, 403, 200, None):
        decision = classify_antigravity_503(status, CAPACITY_BODY)
        assert decision.category == "unknown"
        assert decision.retry_same_account is False


def test_is_model_capacity_exhausted_matches_classifier():
    from src.antigravity_error_classifier import (
        classify_antigravity_503,
        is_model_capacity_exhausted,
    )

    for body in (CAPACITY_BODY, CAPACITY_BY_ERROR_NUMBER, CAPACITY_BY_MESSAGE):
        assert is_model_capacity_exhausted(503, body) is True
    for body in (QUOTA_BODY, OVERLOADED_BODY, GENERIC_UNAVAILABLE_BODY, "", None):
        assert is_model_capacity_exhausted(503, body) is False
        assert classify_antigravity_503(503, body).category != "model_capacity"


@pytest.mark.parametrize(
    "already_retried,requested,retry_enabled,attempt,max_retries,expected",
    [
        (False, True, True, 0, 3, True),    # 第一次容量 503：给一次原号重试
        (True, True, True, 1, 3, False),    # 第二次：透传 503
        (False, False, True, 0, 3, False),  # 分类不允许（配额/服务级）
        (False, True, False, 0, 3, False),  # 重试开关关闭
        (False, True, True, 3, 3, False),   # 预算已用完
    ],
)
def test_same_account_retry_is_single_shot(
    already_retried, requested, retry_enabled, attempt, max_retries, expected
):
    from src.api.antigravity import _should_retry_same_account

    assert (
        _should_retry_same_account(
            requested=requested,
            already_retried=already_retried,
            retry_enabled=retry_enabled,
            attempt=attempt,
            max_retries=max_retries,
        )
        is expected
    )


def test_unparsed_503_body_is_flagged_and_never_retried():
    """看似 JSON 却解析失败的正文：标记 unparsed，按账号级处理，不重试。"""
    from src.antigravity_error_classifier import classify_antigravity_503

    decision = classify_antigravity_503(503, '{"error": {"code": 503, "details": [{')
    assert decision.category == "service_unavailable"
    assert decision.marker == "unparsed"
    assert decision.retry_same_account is False
    assert decision.hold_account is True


def test_capacity_matched_from_raw_text_is_labelled_text():
    """正文解析不了（如 Python repr）但原文带标记：仍判容量，依据标为 text。"""
    from src.antigravity_error_classifier import classify_antigravity_503

    decision = classify_antigravity_503(
        503, "{'error': {'code': 503, 'details': [{'reason': 'MODEL_CAPACITY_EXHAUSTED'}]}}"
    )
    assert decision.category == "model_capacity"
    assert decision.marker == "text"
    assert decision.reason == ""
    assert decision.retry_same_account is True

@pytest.mark.parametrize('reason,category,retry,cooldown,hold', [
    ('QUOTA_EXHAUSTED', 'quota', False, True, False),
    ('RESOURCE_EXHAUSTED', 'quota', False, True, False),
    ('MODEL_OVERLOADED', 'model_overloaded', False, True, True),
    ('MODEL_CAPACITY_EXHAUSTED', 'model_capacity', True, False, False),
])
def test_503_structured_reason_wins_over_conflicting_text_and_number(reason, category, retry, cooldown, hold):
    import json
    from src.antigravity_error_classifier import classify_antigravity_503
    body = {'error': {'message': 'No capacity available; quota exhausted and overloaded',
                     'details': [{'reason': reason, 'metadata': {'error_number': '2010'}}]}}
    decision = classify_antigravity_503(503, json.dumps(body))
    assert (decision.category, decision.retry_same_account, decision.write_cooldown,
            decision.hold_account, decision.marker) == (category, retry, cooldown, hold, 'reason')
