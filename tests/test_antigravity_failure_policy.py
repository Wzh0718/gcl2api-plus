"""失败归因单一入口（failure policy）的行为契约。

背景（2026-11-28 复盘）：同一份 5xx 曾被四条链路各自判定 —— 文本流式、
文本非流式、图像、面板「消息测试」—— 于是同一个 503 可能同时被写成
「账号冷藏 12h」「模型冷却 40s」「轮转降权」，三份结论互相矛盾；配置项
``antigravity_transient_hold_seconds`` 也被常量覆盖。

本文件锁定收敛后的口径：

- 侧车合成 502（上游未应答）只压 60s，不写面板错误码；
- 容量型 503 只降权、不冷藏、不写模型冷却；
- 配额型 503 只冷模型、不冷藏；
- 服务级/未知 503 与 500/502 冷藏配置时长（默认 12h）；
- 存储层按传入窗口写入，不再自行改档；
- 冷却到期转 warmup 只认窗口，不依赖 ``transient_error_count``。
"""

import time

import pytest

from src.antigravity_failure_policy import (
    ACTION_TABLE,
    FailureKind,
    CATEGORY_MODEL_CAPACITY,
    CATEGORY_QUOTA,
    CATEGORY_RELAY_TRANSPORT,
    DEFAULT_ACCOUNT_HOLD_SECONDS,
    RELAY_HOLD_SECONDS,
    apply_action,
    apply_hold,
    classify_failure_kind,
    decide,
    decide_failure,
    resolve_account_hold_seconds,
)

CAPACITY_BODY = (
    '{"error": {"code": 503, "message": "No capacity available for model '
    'gemini-2.5-flash-lite on the server", "status": "UNAVAILABLE", '
    '"details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", '
    '"reason": "MODEL_CAPACITY_EXHAUSTED", '
    '"metadata": {"error_number": "2010", "model": "gemini-2.5-flash-lite"}}, '
    '{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "38s"}]}}'
)
QUOTA_BODY = (
    '{"error": {"code": 503, "message": "Quota exceeded for this project", '
    '"status": "UNAVAILABLE", "details": [{"reason": "QUOTA_EXHAUSTED"}]}}'
)
SERVICE_BODY = (
    '{"error": {"code": 503, "message": "The service is currently unavailable", '
    '"status": "UNAVAILABLE"}}'
)


# ==================== 归因 ====================


def test_relay_transport_failure_only_benches_one_hop():
    disposition = decide(502, "", relay_failure=True)

    assert disposition.category == CATEGORY_RELAY_TRANSPORT
    assert disposition.account_hold_seconds == pytest.approx(RELAY_HOLD_SECONDS)
    assert disposition.record_error_state is False, "传输层失败不得污染账号错误画像"


def test_capacity_503_demotes_without_holding_the_account():
    disposition = decide(503, CAPACITY_BODY)

    assert disposition.category == CATEGORY_MODEL_CAPACITY
    assert disposition.holds_account is False, "容量是上游全局窗口，不归咎账号"
    assert disposition.demote_round_robin is True
    assert disposition.retry_same_account is True
    assert disposition.model_cooldown is False, "容量型不写模型冷却"


def test_quota_503_cools_model_only():
    disposition = decide(503, QUOTA_BODY)

    assert disposition.category == CATEGORY_QUOTA
    assert disposition.holds_account is False
    assert disposition.model_cooldown is True
    assert disposition.demote_round_robin is False


@pytest.mark.parametrize(
    "body", [SERVICE_BODY, "", "{}", '{"error": "broken json']
)
def test_service_level_503_holds_the_account(body):
    disposition = decide(503, body)

    assert disposition.holds_account is True
    assert disposition.account_hold_seconds == pytest.approx(DEFAULT_ACCOUNT_HOLD_SECONDS)
    assert disposition.model_cooldown is True


@pytest.mark.parametrize("status_code", [500, 502])
def test_upstream_5xx_holds_the_account(status_code):
    disposition = decide(status_code, "")

    assert disposition.holds_account is True
    assert disposition.account_hold_seconds == pytest.approx(DEFAULT_ACCOUNT_HOLD_SECONDS)
    assert disposition.model_cooldown is False, "500/502 不解析 Retry-After"


@pytest.mark.parametrize("status_code", [403, 400, 200])
def test_other_status_codes_produce_no_account_disposition(status_code):
    disposition = decide(status_code, SERVICE_BODY)

    assert disposition.holds_account is False
    assert disposition.record_error_state is True


def test_rate_limit_cools_model_and_rotates_without_holding_account():
    disposition = decide(429, SERVICE_BODY)

    assert disposition.kind is FailureKind.RATE_LIMITED
    assert disposition.holds_account is False
    assert disposition.model_cooldown is True
    assert disposition.retry_other_account is True


def test_hold_window_is_configurable_per_call():
    disposition = decide(500, "", account_hold_seconds=120)

    assert disposition.account_hold_seconds == pytest.approx(120)


# ==================== 枚举归一（400/401/403/429/500/502/503） ====================


@pytest.mark.parametrize(
    "status,body,relay,forbidden,expected",
    [
        (502, "", True, "", FailureKind.RELAY_TRANSPORT),
        (400, '{"error": {"message": "invalid argument"}}', False, "", FailureKind.REQUEST_INVALID),
        (400, '{"error": "invalid_grant"}', False, "", FailureKind.REQUEST_INVALID),
        (401, "", False, "", FailureKind.AUTH_INVALID),
        (403, "", False, "account_forbidden", FailureKind.ACCOUNT_FORBIDDEN),
        (403, "", False, "geo_blocked", FailureKind.GEO_BLOCKED),
        (403, "", False, "proxy_drift", FailureKind.PROXY_BINDING),
        (403, "", False, "proxy_failed", FailureKind.PROXY_BINDING),
        (403, "", False, "validation_required", FailureKind.VALIDATION_REQUIRED),
        (403, "", False, "model_forbidden", FailureKind.MODEL_FORBIDDEN),
        (429, "", False, "", FailureKind.RATE_LIMITED),
        (500, "", False, "", FailureKind.UPSTREAM_INTERNAL),
        (502, "", False, "", FailureKind.UPSTREAM_BAD_GATEWAY),
        (503, CAPACITY_BODY, False, "", FailureKind.MODEL_CAPACITY),
        (503, QUOTA_BODY, False, "", FailureKind.QUOTA_EXHAUSTED),
        (503, SERVICE_BODY, False, "", FailureKind.SERVICE_UNAVAILABLE),
        (503, "", False, "", FailureKind.SERVICE_UNAVAILABLE),
        (418, "", False, "", FailureKind.UNKNOWN),
    ],
)
def test_classify_failure_kind_covers_each_status(
    status, body, relay, forbidden, expected,
):
    assert classify_failure_kind(
        status, body, relay_failure=relay, forbidden_category=forbidden,
    ) is expected


def test_every_kind_has_an_action_row():
    for kind in FailureKind:
        assert kind in ACTION_TABLE, f"{kind} 缺少动作行"


def test_unknown_400_does_not_disable_the_account_by_default():
    """400 只按正文分类决定是否停用；普通请求错误不罚账号。"""
    disposition = decide(400, '{"error": {"message": "invalid argument"}}')

    assert disposition.disable_account is False
    assert disposition.holds_account is False
    assert disposition.retry_other_account is False, "400 换号没有意义"


def test_account_forbidden_disables_and_rotates():
    disposition = decide(403, "", forbidden_category="account_forbidden")

    assert disposition.disable_account is True
    assert disposition.retry_other_account is True
    assert disposition.holds_account is False


def test_geo_blocked_does_not_penalize_the_account():
    disposition = decide(403, "", forbidden_category="geo_blocked")

    assert disposition.disable_account is False
    assert disposition.holds_account is False
    assert disposition.retry_other_account is True


# ==================== 配置读取 ====================


@pytest.mark.asyncio
async def test_resolve_hold_seconds_reads_config(monkeypatch):
    import config

    async def fake_seconds():
        return 900

    monkeypatch.setattr(config, "get_antigravity_transient_hold_seconds", fake_seconds)
    assert await resolve_account_hold_seconds() == pytest.approx(900)


@pytest.mark.asyncio
async def test_zero_or_broken_config_falls_back_to_default(monkeypatch):
    import config

    async def zero():
        return 0

    monkeypatch.setattr(config, "get_antigravity_transient_hold_seconds", zero)
    assert await resolve_account_hold_seconds() == pytest.approx(
        DEFAULT_ACCOUNT_HOLD_SECONDS
    )

    async def broken():
        raise RuntimeError("config store offline")

    monkeypatch.setattr(config, "get_antigravity_transient_hold_seconds", broken)
    assert await resolve_account_hold_seconds() == pytest.approx(
        DEFAULT_ACCOUNT_HOLD_SECONDS
    )


@pytest.mark.asyncio
async def test_decide_failure_uses_configured_window(monkeypatch):
    import config

    async def fake_seconds():
        return 120

    monkeypatch.setattr(config, "get_antigravity_transient_hold_seconds", fake_seconds)
    disposition = await decide_failure(500, "")

    assert disposition.account_hold_seconds == pytest.approx(120)


# ==================== 唯一写入点 ====================


class _RecordingManager:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def hold_transient(
        self, credential_name, status_code=None, mode="antigravity",
        seconds=None, relay_failure=False,
    ):
        if self.fail:
            raise RuntimeError("db unavailable")
        self.calls.append((credential_name, status_code, seconds, relay_failure))
        return time.time() + (seconds or 0)


class _LegacyManager:
    """只实现存储层方法名的替身，验证回退路径。"""

    def __init__(self):
        self.calls = []

    async def hold_antigravity_transient(
        self, filename, seconds=600, status_code=None, *, relay_failure=False,
    ):
        self.calls.append((filename, status_code, seconds, relay_failure))
        return time.time() + seconds


@pytest.mark.asyncio
async def test_apply_hold_writes_the_decided_window():
    manager = _RecordingManager()

    await apply_hold(manager, "a.json", decide(500, "", account_hold_seconds=43200))

    assert manager.calls == [("a.json", 500, 43200, False)]


@pytest.mark.asyncio
async def test_apply_hold_marks_relay_window():
    manager = _RecordingManager()

    await apply_hold(manager, "a.json", decide(502, "", relay_failure=True))

    assert manager.calls == [("a.json", 502, RELAY_HOLD_SECONDS, True)]


@pytest.mark.asyncio
async def test_apply_hold_skips_when_nothing_to_hold():
    manager = _RecordingManager()

    await apply_hold(manager, "a.json", decide(503, CAPACITY_BODY))

    assert manager.calls == [], "容量型 503 不得写冷却"


@pytest.mark.asyncio
async def test_apply_hold_falls_back_to_storage_method():
    manager = _LegacyManager()

    await apply_hold(manager, "a.json", decide(500, "", account_hold_seconds=60))

    assert manager.calls == [("a.json", 500, 60, False)]


@pytest.mark.asyncio
async def test_apply_hold_never_raises():
    manager = _RecordingManager(fail=True)

    assert await apply_hold(manager, "a.json", decide(500, "")) == 0.0


# ==================== 统一执行入口（停用 + 冷藏） ====================


class _ActionManager:
    """记录停用与冷藏两个写入动作。"""

    def __init__(self, *, fail_disable=False):
        self.disabled = []
        self.holds = []
        self.fail_disable = fail_disable

    async def set_cred_disabled(self, credential_name, disabled, mode="antigravity"):
        if self.fail_disable:
            raise RuntimeError("db unavailable")
        self.disabled.append((credential_name, disabled, mode))
        return True

    async def hold_transient(
        self, credential_name, status_code=None, mode="antigravity",
        seconds=None, relay_failure=False,
    ):
        self.holds.append((credential_name, status_code, seconds, relay_failure))
        return time.time() + (seconds or 0)


@pytest.mark.asyncio
async def test_apply_action_disables_account_forbidden_without_holding():
    manager = _ActionManager()
    disposition = decide(403, "", forbidden_category="account_forbidden")

    await apply_action(manager, "a.json", disposition)

    assert manager.disabled == [("a.json", True, "antigravity")]
    assert manager.holds == [], "TOS/账号级 403 不冷藏，只停用"


@pytest.mark.asyncio
async def test_apply_action_holds_5xx_without_disabling():
    manager = _ActionManager()
    disposition = decide(500, "", account_hold_seconds=60)

    await apply_action(manager, "a.json", disposition)

    assert manager.disabled == []
    assert manager.holds == [("a.json", 500, 60, False)]


@pytest.mark.asyncio
async def test_apply_action_override_can_skip_disable():
    """未知 403 的停用由处理器按配置判定，执行入口接受覆盖。"""
    manager = _ActionManager()
    disposition = decide(403, "", forbidden_category="account_forbidden")

    await apply_action(manager, "a.json", disposition, disable_account=False)

    assert manager.disabled == []


@pytest.mark.asyncio
async def test_apply_action_never_raises_when_disable_fails():
    manager = _ActionManager(fail_disable=True)
    disposition = decide(403, "", forbidden_category="account_forbidden")

    assert await apply_action(manager, "a.json", disposition) == 0.0


# ==================== 到期恢复与计数解耦 ====================


async def _make_manager(tmp_path, monkeypatch, names=("a.json",)):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    from src.storage.sqlite_manager import SQLiteManager

    manager = SQLiteManager()
    await manager.initialize()
    for name in names:
        await manager.store_credential(name, {"access_token": name}, mode="antigravity")
    return manager


@pytest.mark.asyncio
async def test_expired_hold_still_starts_warmup_after_a_success(tmp_path, monkeypatch):
    """窗口内的成功会清计数；到期恢复不能因此漏掉这个账号。"""
    manager = await _make_manager(tmp_path, monkeypatch)

    await manager.hold_antigravity_transient("a.json", seconds=43200, status_code=500)
    # 面板「消息测试」/ 在飞请求成功：计数清零，窗口保持。
    await manager.record_success("a.json", model_name="gemini-2.5-flash", mode="antigravity")
    # 窗口到期。
    async with __import__("src.storage.sqlite_pragmas", fromlist=["connect"]).connect(
        manager._db_path
    ) as db:
        await db.execute(
            "UPDATE antigravity_credentials SET transient_hold_until = ? WHERE filename = ?",
            (time.time() - 1, "a.json"),
        )
        await db.commit()

    recovered = await manager.recover_expired_transient_holds()

    assert recovered == ["a.json"], "到期窗口必须被清掉并转入养号"
    async with __import__("src.storage.sqlite_pragmas", fromlist=["connect"]).connect(
        manager._db_path
    ) as db:
        async with db.execute(
            "SELECT transient_hold_until, transient_error_count, disabled "
            "FROM antigravity_credentials WHERE filename = ?",
            ("a.json",),
        ) as cursor:
            hold_until, count, disabled = await cursor.fetchone()
        async with db.execute(
            "SELECT status, level FROM antigravity_warmup_accounts "
            "WHERE credential_name = ?",
            ("a.json",),
        ) as cursor:
            warmup = await cursor.fetchone()

    assert hold_until == 0
    assert count == 0
    assert disabled == 0
    assert warmup is not None, "到期账号必须进入 warmup，而不是满速回到轮转"
    assert warmup[1] == 0, "从 level 0（并发 1 / 间隔 5s）起步"


@pytest.mark.asyncio
async def test_unexpired_hold_is_left_alone(tmp_path, monkeypatch):
    manager = await _make_manager(tmp_path, monkeypatch)

    await manager.hold_antigravity_transient("a.json", seconds=43200, status_code=500)

    assert await manager.recover_expired_transient_holds() == []
