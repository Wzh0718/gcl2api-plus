"""侧车合成 502 不得被当作「上游 5xx」冷藏半天。

背景（2026-11-27 生产 47.88.89.58 事故）：一个客户端请求在 9 秒内被换号重试
6 次，六个不同代理节点的传输层全部失败。侧车把 transport 错误写成固定 502
``upstream request failed``，上层按「上游 5xx」处理，于是 6 个账号各吃一次
43200s 冷却 —— 而同一时窗内同账号同节点仍在正常返回 200，证明失败属于单请求
级的传输抖动，不是账号或上游的问题。

新语义：
- 侧车在自己合成的 502 上带 ``X-Agy-Relay-Failure``，正文带原因；
- 这类失败只做 60s 短冷却、不写面板 ``error_codes``、不推进 5xx 阶梯；
- 上游真实返回的 502（不带该头、正文是上游内容）仍走原有半天冷却。
"""

import time

import pytest

from src.api.utils import (
    RELAY_FAILURE_HOLD_SECONDS,
    handle_error_with_retry,
    is_relay_transport_failure,
    relay_failure_tag,
    strip_relay_failure_header,
)
from src.storage.sqlite_manager import SQLiteManager


async def make_manager(tmp_path, monkeypatch, names=("a.json", "b.json")):
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    manager = SQLiteManager()
    await manager.initialize()
    for name in names:
        await manager.store_credential(name, {"access_token": name}, mode="antigravity")
    return manager


async def read_hold(manager, name):
    async with __import__("src.storage.sqlite_pragmas", fromlist=["connect"]).connect(
        manager._db_path
    ) as db:
        async with db.execute(
            "SELECT transient_hold_until, transient_error_count, error_codes, disabled "
            "FROM antigravity_credentials WHERE filename = ?",
            (name,),
        ) as cursor:
            return await cursor.fetchone()


class _RecordingManager:
    """只记录 hold 调用参数的替身，声明完整签名。"""

    def __init__(self):
        self.calls = []

    async def hold_transient(
        self, credential_name, status_code=None, mode="antigravity",
        seconds=None, relay_failure=False,
    ):
        self.calls.append((credential_name, status_code, seconds, relay_failure))
        return time.time() + 60

    async def set_cred_disabled(self, credential_name, disabled, mode="geminicli"):
        raise AssertionError("relay 失败不得封号")

    async def set_model_cooldown(self, *_args, **_kwargs):
        raise AssertionError("relay 失败不得写模型级冷却")


def test_relay_header_wins_over_body_text():
    assert relay_failure_tag({"x-agy-relay-failure": "proxy-connect"}) == "proxy-connect"
    assert is_relay_transport_failure({"x-agy-relay-failure": "client-canceled"}) is True


def test_legacy_relay_body_is_still_recognised():
    # 旧镜像没有标记头，只能靠正文前缀兜底。
    assert is_relay_transport_failure(None, "upstream request failed\n") is True
    assert is_relay_transport_failure(None, "upstream request failed: proxy-timeout: ...") is True


def test_upstream_returned_error_is_not_a_relay_failure():
    body = '{"error": {"code": 502, "message": "backend error"}}'
    assert is_relay_transport_failure(None, body) is False
    assert is_relay_transport_failure({"content-type": "application/json"}, body) is False


def test_relay_header_is_stripped_before_forwarding():
    headers = {"Content-Type": "text/plain", "X-Agy-Relay-Failure": "proxy-timeout"}
    strip_relay_failure_header(headers)
    assert all(key.lower() != "x-agy-relay-failure" for key in headers)


@pytest.mark.asyncio
async def test_relay_failure_holds_short_window_only():
    manager = _RecordingManager()
    should_retry = await handle_error_with_retry(
        manager, 502, "a.json", True, 1, 3, mode="antigravity", relay_failure=True
    )
    assert should_retry is True, "传输层失败仍应换号重试"
    assert manager.calls == [
        ("a.json", 502, RELAY_FAILURE_HOLD_SECONDS, True)
    ], "relay 失败必须走短冷却，而不是 12h"


@pytest.mark.asyncio
async def test_relay_failure_without_retry_budget_stops_retrying():
    manager = _RecordingManager()
    should_retry = await handle_error_with_retry(
        manager, 502, "a.json", True, 3, 3, mode="antigravity", relay_failure=True
    )
    assert should_retry is False
    assert manager.calls == [("a.json", 502, RELAY_FAILURE_HOLD_SECONDS, True)]


@pytest.mark.asyncio
async def test_real_upstream_5xx_keeps_half_day_hold(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    before = time.time()
    hold_until = await manager.hold_antigravity_transient(
        "a.json", seconds=600, status_code=502
    )
    assert hold_until == pytest.approx(before + 43200, abs=5), (
        "上游真实 502 保持原有半天冷却，本次改动不得放宽"
    )


@pytest.mark.asyncio
async def test_relay_502_only_benches_account_for_a_minute(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    before = time.time()
    hold_until = await manager.hold_antigravity_transient(
        "a.json", seconds=600, status_code=502, relay_failure=True
    )
    assert hold_until == pytest.approx(before + RELAY_FAILURE_HOLD_SECONDS, abs=5)
    stored_until, count, _error_codes, disabled = await read_hold(manager, "a.json")
    assert stored_until == pytest.approx(hold_until)
    assert count == 1
    assert disabled == 0


@pytest.mark.asyncio
async def test_relay_502_never_extends_an_existing_hold(tmp_path, monkeypatch):
    manager = await make_manager(tmp_path, monkeypatch)
    long_until = await manager.hold_antigravity_transient(
        "a.json", seconds=600, status_code=500
    )
    again = await manager.hold_antigravity_transient(
        "a.json", seconds=600, status_code=502, relay_failure=True
    )
    assert again == pytest.approx(long_until), "传输抖动不得延长已有的上游冷却"


@pytest.mark.asyncio
async def test_relay_failure_does_not_write_panel_error_codes(tmp_path, monkeypatch):
    from src.credential_manager import CredentialManager

    manager = await make_manager(tmp_path, monkeypatch)
    credential_manager = CredentialManager()
    credential_manager._storage_adapter = manager
    credential_manager._initialized = True

    await credential_manager.record_api_call_result(
        "a.json", False, error_code=502,
        mode="antigravity", model_name="gemini-2.5-flash-lite",
        error_message="upstream request failed: client-canceled",
        record_error_state=False,
    )

    _until, _count, error_codes, _disabled = await read_hold(manager, "a.json")
    assert "502" not in str(error_codes), "传输层失败不得在面板留下「错误码: 502」"


@pytest.mark.asyncio
async def test_real_upstream_502_still_records_error_code(tmp_path, monkeypatch):
    from src.credential_manager import CredentialManager

    manager = await make_manager(tmp_path, monkeypatch)
    credential_manager = CredentialManager()
    credential_manager._storage_adapter = manager
    credential_manager._initialized = True

    await credential_manager.record_api_call_result(
        "a.json", False, error_code=502,
        mode="antigravity", model_name="gemini-2.5-flash-lite",
        error_message='{"error":{"code":502}}',
    )

    _until, _count, error_codes, _disabled = await read_hold(manager, "a.json")
    assert "502" in str(error_codes), "上游真实 502 的可见性保持不变"
