from src.converter.gemini_fix import (
    _ensure_empty_tool_schema_for_claude,
    map_antigravity_gemini_model,
)


def test_antigravity_claude_tools_keep_schema_in_parameters():
    tools = [
        {
            "functionDeclarations": [
                {
                    "name": "test_tool",
                    "description": "A test tool.",
                    "parametersJsonSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                }
            ]
        }
    ]

    result = _ensure_empty_tool_schema_for_claude(tools, "claude-opus-4-6-thinking", "antigravity")
    declaration = result[0]["functionDeclarations"][0]

    assert declaration["parameters"]["type"] == "object"
    assert "parametersJsonSchema" not in declaration


def test_map_antigravity_gemini_model_36_suffix_variants():
    """3.6-flash 的 -low/-medium/-high 后缀应保留为精确后端模型 ID（修复 404）。"""
    assert map_antigravity_gemini_model("gemini-3.6-flash-low", None, None) == "gemini-3.6-flash-low"
    assert map_antigravity_gemini_model("gemini-3.6-flash-medium", None, None) == "gemini-3.6-flash-medium"
    assert map_antigravity_gemini_model("gemini-3.6-flash-high", None, None) == "gemini-3.6-flash-high"


def test_map_antigravity_gemini_model_tiered_passthrough():
    assert map_antigravity_gemini_model("gemini-3.6-flash-tiered", None, None) == "gemini-3.6-flash-tiered"
    assert map_antigravity_gemini_model("gemini-3.7-flash-tiered", None, None) == "gemini-3.7-flash-tiered"


def test_map_antigravity_gemini_model_bare_names_with_thinking_level():
    """裸名按 thinkingLevel 映射，对齐 agy CLI 的 --effort 行为。"""
    assert map_antigravity_gemini_model("gemini-3.6-flash", None, None) == "gemini-3.6-flash-low"
    assert map_antigravity_gemini_model("gemini-3.6-flash", "HIGH", None) == "gemini-3.6-flash-high"
    assert map_antigravity_gemini_model("gemini-3.6-flash", "MEDIUM", None) == "gemini-3.6-flash-medium"
    assert map_antigravity_gemini_model("gemini-3.6-flash", "LOW", None) == "gemini-3.6-flash-low"
    # 3.7-flash 上游只有 tiered 一档
    assert map_antigravity_gemini_model("gemini-3.7-flash", None, None) == "gemini-3.7-flash-tiered"
    assert map_antigravity_gemini_model("gemini-3.7-flash", "HIGH", None) == "gemini-3.7-flash-tiered"


def test_map_antigravity_gemini_model_existing_models_unchanged():
    assert map_antigravity_gemini_model("gemini-2.5-flash", None, None) == "gemini-2.5-flash"
    assert map_antigravity_gemini_model("gemini-3.1-flash-lite", None, None) == "gemini-3.1-flash-lite"
    assert map_antigravity_gemini_model("gemini-3.5-flash-low", None, None) == "gemini-3.5-flash-low"
    assert map_antigravity_gemini_model("gemini-3-flash-x", None, None) == "gemini-3-flash-agent"
