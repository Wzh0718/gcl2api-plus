"""
敏感词替换模块测试

覆盖：
- parse_sensitive_word_rules: 规则文本解析（注释、空行、=> 分隔、删除语义）
- apply_sensitive_word_rules: systemInstruction 替换与写时复制语义
- filter_request_system_instruction: 配置开关与规则驱动的异步过滤入口
- normalize_gemini_request 集成：geminicli/antigravity 生效、vertex 不生效
"""

import config
import pytest


@pytest.fixture(autouse=True)
def isolate_filter_settings(monkeypatch):
    async def rewrite():
        return "rewrite"
    async def no_defaults():
        return False
    async def thoughts():
        return True
    monkeypatch.setattr(config, "get_return_thoughts_to_frontend", thoughts)
    monkeypatch.setattr(config, "get_sensitive_word_filter_mode", rewrite)
    monkeypatch.setattr(config, "get_sensitive_word_use_default_keywords", no_defaults)

from src.converter.gemini_fix import normalize_gemini_request
from src.converter.sensitive_words import (
    apply_sensitive_word_rules,
    filter_request_system_instruction,
    parse_sensitive_word_rules,
)


# ==================== 规则解析 ====================

def test_parse_rules_empty_input():
    assert parse_sensitive_word_rules("") == []
    assert parse_sensitive_word_rules(None) == []
    assert parse_sensitive_word_rules("   \n  \n") == []
    assert parse_sensitive_word_rules(123) == []


def test_parse_rules_basic_forms():
    rules = parse_sensitive_word_rules(
        "Claude Code=>Antigravity\n"
        "Claude Agent SDK=>\n"
        "Hermes Agent\n"
        "# 这是注释\n"
        "\n"
        "   # 缩进注释\n"
    )
    assert rules == [
        ("Claude Code", "Antigravity"),
        ("Claude Agent SDK", ""),
        ("Hermes Agent", ""),
    ]


def test_parse_rules_strips_whitespace_and_splits_once():
    rules = parse_sensitive_word_rules("  foo  =>  bar=>baz  ")
    assert rules == [("foo", "bar=>baz")]


def test_parse_rules_skips_empty_find():
    rules = parse_sensitive_word_rules("=>x\n \nok")
    assert rules == [("ok", "")]


def test_parse_rules_capped_at_max():
    text = "\n".join(f"word{i}" for i in range(500))
    rules = parse_sensitive_word_rules(text)
    assert len(rules) == 200


# ==================== 替换应用 ====================

def test_apply_replaces_in_text_parts():
    request = {
        "systemInstruction": {
            "parts": [
                {"text": "You are Claude Code, an assistant."},
                {"text": "no match here"},
            ]
        }
    }
    hits = apply_sensitive_word_rules(request, [("Claude Code", "Antigravity")])
    assert hits == 1
    assert request["systemInstruction"]["parts"][0]["text"] == "You are Antigravity, an assistant."
    assert request["systemInstruction"]["parts"][1]["text"] == "no match here"


def test_apply_delete_semantics_without_separator():
    request = {"systemInstruction": {"parts": [{"text": "abc Hermes Agent def"}]}}
    hits = apply_sensitive_word_rules(request, [("Hermes Agent", "")])
    assert hits == 1
    assert request["systemInstruction"]["parts"][0]["text"] == "abc  def"


def test_apply_counts_multiple_occurrences():
    request = {"systemInstruction": {"parts": [{"text": "xx foo yy foo"}]}}
    hits = apply_sensitive_word_rules(request, [("foo", "bar")])
    assert hits == 2
    assert request["systemInstruction"]["parts"][0]["text"] == "xx bar yy bar"


def test_apply_is_case_insensitive():
    request = {"systemInstruction": {"parts": [{"text": "claude code lower"}]}}
    hits = apply_sensitive_word_rules(request, [("Claude Code", "x")])
    assert hits == 1
    assert request["systemInstruction"]["parts"][0]["text"] == "x lower"


def test_apply_copy_on_write_keeps_original_part_objects():
    original_part = {"text": "You are Claude Code.", "extra": 1}
    instruction = {"parts": [original_part]}
    request = {"systemInstruction": instruction}

    hits = apply_sensitive_word_rules(request, [("Claude Code", "Antigravity")])
    assert hits == 1
    # 原嵌套对象不被修改
    assert original_part["text"] == "You are Claude Code."
    # 新对象保留了其他字段
    new_part = request["systemInstruction"]["parts"][0]
    assert new_part["text"] == "You are Antigravity."
    assert new_part["extra"] == 1
    assert new_part is not original_part


def test_apply_no_match_leaves_request_untouched():
    part = {"text": "nothing to replace"}
    request = {"systemInstruction": {"parts": [part]}}
    hits = apply_sensitive_word_rules(request, [("Claude Code", "x")])
    assert hits == 0
    # 未命中时不重建对象
    assert request["systemInstruction"]["parts"][0] is part


def test_apply_handles_string_parts():
    request = {"systemInstruction": {"parts": ["plain Claude Code text"]}}
    hits = apply_sensitive_word_rules(request, [("Claude Code", "Antigravity")])
    assert hits == 1
    assert request["systemInstruction"]["parts"][0] == {"text": "plain Antigravity text"}


def test_apply_handles_string_instruction():
    request = {"systemInstruction": "You are Claude Code."}
    hits = apply_sensitive_word_rules(request, [("Claude Code", "Antigravity")])
    assert hits == 1
    assert request["systemInstruction"] == {"parts": [{"text": "You are Antigravity."}]}


def test_apply_supports_legacy_system_instructions_key():
    request = {"system_instructions": {"parts": [{"text": "Claude Code here"}]}}
    hits = apply_sensitive_word_rules(request, [("Claude Code", "Antigravity")])
    assert hits == 1
    assert request["system_instructions"]["parts"][0]["text"] == "Antigravity here"
    assert "systemInstruction" not in request


def test_apply_ignores_message_contents():
    request = {
        "contents": [{"role": "user", "parts": [{"text": "tell me about Claude Code"}]}],
        "systemInstruction": {"parts": [{"text": "safe"}]},
    }
    hits = apply_sensitive_word_rules(request, [("Claude Code", "Antigravity")])
    assert hits == 0
    assert request["contents"][0]["parts"][0]["text"] == "tell me about Claude Code"


def test_apply_no_instruction_or_rules():
    assert apply_sensitive_word_rules({}, [("a", "b")]) == 0
    assert apply_sensitive_word_rules({"systemInstruction": {"parts": [{"text": "a"}]}}, []) == 0
    assert apply_sensitive_word_rules("not-a-dict", [("a", "b")]) == 0


# ==================== 异步过滤入口 ====================

async def test_filter_skipped_when_disabled(monkeypatch):
    async def _disabled():
        return False

    async def _rules():
        return "Claude Code=>Antigravity"

    monkeypatch.setattr(config, "get_sensitive_word_replace_enabled", _disabled)
    monkeypatch.setattr(config, "get_sensitive_word_replace_rules", _rules)

    request = {"systemInstruction": {"parts": [{"text": "You are Claude Code."}]}}
    await filter_request_system_instruction(request, mode="antigravity")
    assert request["systemInstruction"]["parts"][0]["text"] == "You are Claude Code."


async def test_filter_applies_when_enabled(monkeypatch):
    async def _enabled():
        return True

    async def _rules():
        return "# comment\nClaude Code=>Antigravity\nHermes Agent"

    monkeypatch.setattr(config, "get_sensitive_word_replace_enabled", _enabled)
    monkeypatch.setattr(config, "get_sensitive_word_replace_rules", _rules)

    request = {
        "systemInstruction": {
            "parts": [{"text": "You are Claude Code, aka Hermes Agent."}]
        }
    }
    await filter_request_system_instruction(request, mode="antigravity")
    assert (
        request["systemInstruction"]["parts"][0]["text"]
        == "You are Antigravity, aka ."
    )


async def test_filter_noop_with_empty_rules(monkeypatch):
    async def _enabled():
        return True

    async def _rules():
        return ""

    monkeypatch.setattr(config, "get_sensitive_word_replace_enabled", _enabled)
    monkeypatch.setattr(config, "get_sensitive_word_replace_rules", _rules)

    request = {"systemInstruction": {"parts": [{"text": "You are Claude Code."}]}}
    await filter_request_system_instruction(request, mode="geminicli")
    assert request["systemInstruction"]["parts"][0]["text"] == "You are Claude Code."


# ==================== normalize_gemini_request 集成 ====================

def _patch_sensitive_word_config(monkeypatch, enabled, rules):
    async def _enabled():
        return enabled

    async def _rules():
        return rules

    monkeypatch.setattr(config, "get_sensitive_word_replace_enabled", _enabled)
    monkeypatch.setattr(config, "get_sensitive_word_replace_rules", _rules)


async def test_normalize_applies_filter_in_antigravity_mode(monkeypatch):
    _patch_sensitive_word_config(monkeypatch, True, "Claude Code=>Antigravity")
    request = {
        "model": "gemini-3.1-flash",
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "systemInstruction": {"parts": [{"text": "You are Claude Code."}]},
    }
    result = await normalize_gemini_request(request, mode="antigravity")
    assert result["systemInstruction"]["parts"][0]["text"] == "You are Antigravity."


async def test_normalize_applies_filter_in_geminicli_mode(monkeypatch):
    _patch_sensitive_word_config(monkeypatch, True, "Claude Code=>Antigravity")
    request = {
        "model": "gemini-2.5-flash",
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "systemInstruction": {"parts": [{"text": "You are Claude Code."}]},
    }
    result = await normalize_gemini_request(request, mode="geminicli")
    assert result["systemInstruction"]["parts"][0]["text"] == "You are Antigravity."


async def test_normalize_skips_filter_in_vertex_mode(monkeypatch):
    _patch_sensitive_word_config(monkeypatch, True, "Claude Code=>Antigravity")
    request = {
        "model": "gemini-2.5-flash",
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "systemInstruction": {"parts": [{"text": "You are Claude Code."}]},
    }
    result = await normalize_gemini_request(request, mode="vertex")
    assert result["systemInstruction"]["parts"][0]["text"] == "You are Claude Code."


def test_case_insensitive_longest_match_without_cascading():
    request = {"systemInstruction": "CLAUDE CODE and Claude"}
    hits = apply_sensitive_word_rules(request, [("Claude", "short"), ("Claude Code", "Claude")])
    assert hits == 2
    assert request["systemInstruction"]["parts"][0]["text"] == "Claude and short"


async def test_builtin_rewrite_and_custom_override(monkeypatch):
    _patch_sensitive_word_config(monkeypatch, True, "codex=>My Assistant")
    monkeypatch.setattr(config, "get_sensitive_word_filter_mode", _rewrite, raising=False)
    monkeypatch.setattr(config, "get_sensitive_word_use_default_keywords", _enabled, raising=False)
    request = {"systemInstruction": "CODEX and cursor and CLAUDE CODE"}
    await filter_request_system_instruction(request, mode="antigravity")
    assert request["systemInstruction"]["parts"][0]["text"] == "My Assistant and Antigravity and Antigravity"


async def _rewrite():
    return "rewrite"


async def _enabled():
    return True


async def test_block_rejects_without_mutating_request(monkeypatch):
    import copy
    import pytest
    from fastapi import HTTPException

    _patch_sensitive_word_config(monkeypatch, True, "custom client=>")
    async def block():
        return "block"
    monkeypatch.setattr(config, "get_sensitive_word_filter_mode", block, raising=False)
    monkeypatch.setattr(config, "get_sensitive_word_use_default_keywords", _enabled, raising=False)
    for name in ("cUrSoR", "CUSTOM CLIENT"):
        request = {"systemInstruction": {"parts": [{"text": name}]}}
        before = copy.deepcopy(request)
        with pytest.raises(HTTPException) as exc:
            await filter_request_system_instruction(request, mode="antigravity")
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "blocked_by_antigravity_coding_filter"
        assert request == before


async def test_block_ignores_user_text(monkeypatch):
    _patch_sensitive_word_config(monkeypatch, True, "")
    async def block():
        return "block"
    monkeypatch.setattr(config, "get_sensitive_word_filter_mode", block, raising=False)
    monkeypatch.setattr(config, "get_sensitive_word_use_default_keywords", _enabled, raising=False)
    request = {"contents": [{"role": "user", "parts": [{"text": "Cursor"}]}]}
    await filter_request_system_instruction(request, mode="antigravity")


@pytest.mark.parametrize("compatibility", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["openai", "anthropic", "gemini", "responses"])
async def test_block_http_403_before_upstream(monkeypatch, stream, protocol, compatibility):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.router.antigravity import openai, anthropic, gemini, responses
    from src.api_keys import ApiKeyPrincipal
    from src.api import antigravity as upstream

    async def setting(key, default=None, env_var=None):
        return compatibility if key == "compatibility_mode_enabled" else default
    async def block():
        return "block"
    async def unexpected_upstream(*args, **kwargs):
        pytest.fail("Blocked request reached upstream")
    monkeypatch.setattr(config, "get_config_value", setting)
    _patch_sensitive_word_config(monkeypatch, True, "")
    monkeypatch.setattr(config, "get_sensitive_word_filter_mode", block)
    monkeypatch.setattr(config, "get_sensitive_word_use_default_keywords", _enabled)
    monkeypatch.setattr(upstream, "non_stream_request", unexpected_upstream)
    monkeypatch.setattr(upstream, "stream_request", unexpected_upstream)
    app = FastAPI()
    for module in (openai, anthropic, gemini, responses):
        app.include_router(module.router)
    async def principal():
        return ApiKeyPrincipal(api_key_id="test", name="Test", kind="managed", key_prefix="test")
    # Other tests reload src.utils; override the function each router captured.
    for module in (openai, anthropic, gemini, responses):
        app.dependency_overrides[module.authenticate_antigravity_key] = principal
    if protocol == "openai":
        path = "/v1/chat/completions"
        body = {"model": "gemini-3-pro", "stream": stream, "messages": [
            {"role": "system", "content": "You are cUrSoR."}, {"role": "user", "content": "hello"}
        ]}
    elif protocol == "anthropic":
        path = "/v1/messages"
        body = {"model": "gemini-3-pro", "stream": stream, "max_tokens": 100,
                "system": "You are cUrSoR.", "messages": [{"role": "user", "content": "hello"}]}
    elif protocol == "responses":
        path = "/v1/responses"
        body = {"model": "gemini-3-pro", "stream": stream, "instructions": "You are cUrSoR.", "input": "hello"}
    else:
        method = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1beta/models/gemini-3-pro:{method}"
        body = {"systemInstruction": {"parts": [{"text": "You are cUrSoR."}]},
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "blocked_by_antigravity_coding_filter"


async def test_compatibility_rewrite_preserves_user_and_tool_content(monkeypatch):
    from src.converter.sensitive_words import filter_compatibility_system_prompts

    monkeypatch.setattr(config, "get_compatibility_mode_enabled", _enabled)
    _patch_sensitive_word_config(monkeypatch, True, "")
    monkeypatch.setattr(config, "get_sensitive_word_use_default_keywords", _enabled)
    user = {"role": "user", "content": "Explain Cursor"}
    tool = {"role": "tool", "content": "Codex output", "tool_call_id": "call_1"}
    request = {"system": [{"type": "text", "text": "CLAUDE CODE", "cache_control": {"type": "ephemeral"}}],
               "messages": [{"role": "system", "content": "CODEX"}, user, tool]}
    await filter_compatibility_system_prompts(request, mode="antigravity")
    assert request["system"][0]["text"] == "Antigravity"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["messages"][0]["content"] == "Antigravity"
    assert request["messages"][1:] == [user, tool]


@pytest.mark.parametrize("values", [
    {"sensitive_word_filter_mode": "invalid"},
    {"sensitive_word_use_default_keywords": "false"},
])
async def test_panel_rejects_invalid_filter_settings(values):
    from fastapi import HTTPException
    from src.models import ConfigSaveRequest
    from src.panel.config_routes import save_config

    with pytest.raises(HTTPException) as exc:
        await save_config(ConfigSaveRequest(config=values), token="test")
    assert exc.value.status_code == 400
