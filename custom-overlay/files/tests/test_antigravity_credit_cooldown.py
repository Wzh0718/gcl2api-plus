"""Tests for transient Antigravity 429 recovery."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from src.api.antigravity import (
    CREDITS_EXHAUSTED_MARKER,
    RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS,
    _resolve_error_cooldown,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _error_body(reason: str) -> str:
    return (
        '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": ['
        '{"@type": "type.googleapis.com/google.rpc.ErrorInfo", '
        f'"reason": "{reason}"'
        "}]}}"
    )


@pytest.mark.asyncio
async def test_credits_exhausted_without_reset_time_gets_short_cooldown():
    """Credit 429 没有明确重置时间时也只是短暂限流。"""
    before = time.time()
    cooldown = await _resolve_error_cooldown(429, _error_body(CREDITS_EXHAUSTED_MARKER))
    assert cooldown is not None
    assert before + RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS - 1 <= cooldown
    assert cooldown < before + RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS + 2


@pytest.mark.asyncio
async def test_plain_429_gets_short_cooldown():
    """普通 429（无重置时间、非 credit 耗尽）按 60s 瞬时限流。"""
    before = time.time()
    cooldown = await _resolve_error_cooldown(429, _error_body("RATE_LIMIT_EXCEEDED"))
    assert cooldown is not None
    assert before + RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS - 1 <= cooldown
    assert cooldown < before + RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS + 2


@pytest.mark.asyncio
async def test_429_with_reset_timestamp_uses_reset_time():
    """错误体带 quotaResetTimeStamp 时按重置时间冷却，优先于其他分支。"""
    body = (
        '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": ['
        '{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "QUOTA_EXHAUSTED", '
        '"metadata": {"quotaResetTimeStamp": "2099-01-01T00:00:00Z"}}]}}'
    )
    cooldown = await _resolve_error_cooldown(429, body)
    assert cooldown is not None
    # 2099-01-01 远未来，应远大于瞬时限流分支的值
    assert cooldown > time.time() + RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS


@pytest.mark.asyncio
async def test_503_without_body_no_cooldown():
    """503 无错误体时不设冷却。"""
    cooldown = await _resolve_error_cooldown(503, None)
    assert cooldown is None


@pytest.mark.asyncio
async def test_429_without_body_gets_short_cooldown():
    """429 无错误体时按瞬时限流。"""
    before = time.time()
    cooldown = await _resolve_error_cooldown(429, None)
    assert cooldown is not None
    assert cooldown >= before + RATE_LIMIT_DEFAULT_COOLDOWN_SECONDS - 1


def _run_common_js_slice(start_marker: str, end_marker: str, setup: str, expression: str):
    source = (REPO_ROOT / "front" / "common.js").read_text(encoding="utf-8")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    script = f"{setup}\n{source[start:end]}\n{expression}"
    result = subprocess.run(
        ["node", "-e", script],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    return json.loads(result.stdout)


def test_visible_429_error_expires_with_its_short_cooldown():
    values = _run_common_js_slice(
        "function getVisibleCredentialErrorCodes",
        "// 凭证卡片创建（通用）",
        "",
        """
        console.log(JSON.stringify([
            getVisibleCredentialErrorCodes({error_codes: [429]}, {m: 1060}, 'antigravity', 1000),
            getVisibleCredentialErrorCodes({error_codes: [429]}, {m: 999}, 'antigravity', 1000),
            getVisibleCredentialErrorCodes({error_codes: [403]}, {}, 'antigravity', 1000),
            getVisibleCredentialErrorCodes({error_codes: [429]}, {}, 'normal', 1000)
        ]));
        """,
    )

    assert values == [[429], [], [403], [429]]


def test_cooldown_timer_rerenders_antigravity_credentials_after_expiry():
    values = _run_common_js_slice(
        "function updateCooldownDisplays",
        "// 版本信息管理",
        """
        let normalRenders = 0;
        let antigravityRenders = 0;
        const AppState = {
            creds: {type: 'normal', data: {}, renderList() { normalRenders += 1; }},
            antigravityCreds: {
                type: 'antigravity',
                data: {
                    'account.json': {
                        status: {error_codes: [429]},
                        model_cooldowns: {m: 999}
                    }
                },
                renderList() { antigravityRenders += 1; }
            }
        };
        const document = {querySelectorAll() { return []; }};
        Date.now = () => 1000 * 1000;
        """,
        """
        updateCooldownDisplays();
        console.log(JSON.stringify([
            normalRenders,
            antigravityRenders,
            AppState.antigravityCreds.data['account.json'].status.error_codes,
            AppState.antigravityCreds.data['account.json'].model_cooldowns
        ]));
        """,
    )

    assert values == [0, 1, [], {}]
