"""
敏感词替换模块测试

覆盖：
- parse_sensitive_word_rules: 规则文本解析（注释、空行、=> 分隔、删除语义）
- apply_sensitive_word_rules: systemInstruction 替换与写时复制语义
- filter_request_system_instruction: 配置开关与规则驱动的异步过滤入口
- normalize_gemini_request 集成：geminicli/antigravity 生效、vertex 不生效
"""

import config
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


def test_apply_is_case_sensitive():
    request = {"systemInstruction": {"parts": [{"text": "claude code lower"}]}}
    hits = apply_sensitive_word_rules(request, [("Claude Code", "x")])
    assert hits == 0


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
