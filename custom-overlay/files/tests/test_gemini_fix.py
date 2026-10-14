from src.converter.gemini_fix import (
    _clean_parameters_json_schema,
    _ensure_empty_tool_schema_for_claude,
    _normalize_tools_for_internal_api,
    map_antigravity_gemini_model,
)
from src.converter.openai2gemini import (
    _clean_schema_for_gemini,
    _clean_schema_for_parameters_json_schema,
)
from src.converter.anthropic2gemini import clean_json_schema


# Google 前端严格校验 Schema proto，遇到未知关键字直接 400
# （线上事故：tools[0].function_declarations[105].parameters 携带 dependentRequired）
_GOOGLE_UNSUPPORTED_KEYS = {"dependentRequired", "dependentSchemas"}


def _assert_no_unsupported_keys(schema):
    """递归断言清洗结果中不含 Google 不支持的 schema 关键字。"""
    if isinstance(schema, dict):
        for key, value in schema.items():
            assert key not in _GOOGLE_UNSUPPORTED_KEYS, f"未清洗掉的关键字: {key}"
            _assert_no_unsupported_keys(value)
    elif isinstance(schema, list):
        for item in schema:
            _assert_no_unsupported_keys(item)


def _schema_with_dependent_keys():
    return {
        "type": "object",
        "dependentRequired": {"a": ["b"]},
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "string"},
            "nested": {
                "type": "object",
                "dependentSchemas": {"x": {"required": ["y"]}},
                "properties": {
                    "x": {"type": "string"},
                    "y": {"type": "string"},
                },
            },
            "items_prop": {
                "type": "array",
                "items": {"type": "object", "dependentRequired": {"m": ["n"]}},
            },
        },
    }


def test_clean_parameters_json_schema_strips_dependent_keys():
    cleaned = _clean_parameters_json_schema(_schema_with_dependent_keys())
    _assert_no_unsupported_keys(cleaned)
    assert cleaned["properties"]["a"]["type"] == "string"


def test_openai2gemini_cleaners_strip_dependent_keys():
    _assert_no_unsupported_keys(
        _clean_schema_for_parameters_json_schema(_schema_with_dependent_keys())
    )
    _assert_no_unsupported_keys(_clean_schema_for_gemini(_schema_with_dependent_keys()))


def test_anthropic2gemini_cleaner_strips_dependent_keys():
    _assert_no_unsupported_keys(clean_json_schema(_schema_with_dependent_keys()))


def test_normalize_tools_for_internal_api_strips_dependent_keys():
    tools = [
        {
            "function_declarations": [
                {
                    "name": "test_tool",
                    "description": "A test tool.",
                    "parameters": _schema_with_dependent_keys(),
                }
            ]
        }
    ]
    result = _normalize_tools_for_internal_api(tools)
    declaration = result[0]["functionDeclarations"][0]
    _assert_no_unsupported_keys(declaration["parametersJsonSchema"])


def test_ensure_tool_schema_gemini_parameters_strip_dependent_keys():
    """复现线上 400：Gemini 模型最终 parameters 不得携带 dependent* 关键字。"""
    tools = [
        {
            "functionDeclarations": [
                {
                    "name": "test_tool",
                    "description": "A test tool.",
                    "parametersJsonSchema": _schema_with_dependent_keys(),
                }
            ]
        }
    ]
    result = _ensure_empty_tool_schema_for_claude(tools, "gemini-2.5-flash", "antigravity")
    declaration = result[0]["functionDeclarations"][0]
    assert "parametersJsonSchema" not in declaration
    _assert_no_unsupported_keys(declaration["parameters"])


def test_ensure_tool_schema_custom_tool_strip_dependent_keys():
    """Anthropic 原生 custom 工具格式此前绕过清洗，也需覆盖。"""
    tools = [
        {
            "custom": {
                "name": "test_tool",
                "description": "A test tool.",
                "input_schema": _schema_with_dependent_keys(),
            }
        }
    ]
    for model in ("gemini-2.5-flash", "claude-opus-4-6-thinking"):
        result = _ensure_empty_tool_schema_for_claude(tools, model, "antigravity")
        declaration = result[0]["functionDeclarations"][0]
        _assert_no_unsupported_keys(declaration["parameters"])


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
    # 3.7-flash 裸名默认 tiered 自动档；显式档位透传（上游 >= 1.1.28 已下发独立档 ID）
    assert map_antigravity_gemini_model("gemini-3.7-flash", None, None) == "gemini-3.7-flash-tiered"
    assert map_antigravity_gemini_model("gemini-3.7-flash", "HIGH", None) == "gemini-3.7-flash-high"
    assert map_antigravity_gemini_model("gemini-3.7-flash-high", None, None) == "gemini-3.7-flash-high"
    assert map_antigravity_gemini_model("gemini-3.7-flash-medium", None, None) == "gemini-3.7-flash-medium"


def test_map_antigravity_gemini_model_existing_models_unchanged():
    assert map_antigravity_gemini_model("gemini-2.5-flash", None, None) == "gemini-2.5-flash"
    assert map_antigravity_gemini_model("gemini-3.1-flash-lite", None, None) == "gemini-3.1-flash-lite"
    assert map_antigravity_gemini_model("gemini-3.5-flash-low", None, None) == "gemini-3.5-flash-low"
    # 未登记家族（非档位后缀的非基础名）原样透传，不再强行归入 -agent
    assert map_antigravity_gemini_model("gemini-3-flash-x", None, None) == "gemini-3-flash-x"
    # 遗留 -preview 裸名仍归入家族默认档
    assert map_antigravity_gemini_model("gemini-3-flash-preview", None, None) == "gemini-3-flash-agent"
    assert map_antigravity_gemini_model("gemini-3.1-pro-preview", None, None) == "gemini-3.1-pro-low"


def test_map_antigravity_gemini_model_tier_suffix_passthrough_for_all_families():
    """所有带上游档位后缀的请求名一律原样透传（数据驱动统一规则）。"""
    cases = [
        "gemini-3.1-pro-low",
        "gemini-3.5-flash-extra-low",
        "gemini-3.5-flash-low",
        "gemini-3.6-flash-tiered",
        "gemini-3.7-flash-high",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "gemini-3.8-flash-tiered",
        # 未来新家族同样透传，无需改代码
        "gemini-3.9-flash-high",
    ]
    for name in cases:
        assert map_antigravity_gemini_model(name, None, None) == name


def test_map_antigravity_gemini_model_unregistered_family_passthrough():
    """未登记家族原样透传：修复 gemini-3.5-flash-lite 被改写成 flash-low 的 bug。"""
    assert map_antigravity_gemini_model("gemini-3.5-flash-lite", None, None) == "gemini-3.5-flash-lite"
    assert map_antigravity_gemini_model("gemini-3.1-flash-image", None, None) == "gemini-3.1-flash-image"
    assert map_antigravity_gemini_model("gemini-pro-agent", None, None) == "gemini-pro-agent"


def test_map_antigravity_gemini_model_31_pro_high_override():
    """gemini-3.1-pro-high 后端已废弃，等价改写为 gemini-pro-agent（两条路径均覆盖）。"""
    assert map_antigravity_gemini_model("gemini-3.1-pro-high", None, None) == "gemini-pro-agent"
    assert map_antigravity_gemini_model("gemini-3.1-pro", "HIGH", None) == "gemini-pro-agent"
    assert map_antigravity_gemini_model("gemini-3.1-pro", None, None) == "gemini-3.1-pro-low"


def test_map_antigravity_gemini_model_25_tier_suffix_uses_thinking_config():
    """2.5 系档位走 thinkingConfig，模型名恒为基础名。"""
    assert map_antigravity_gemini_model("gemini-2.5-flash-high", None, None) == "gemini-2.5-flash"
    assert map_antigravity_gemini_model("gemini-2.5-flash-thinking", None, None) == "gemini-2.5-flash-thinking"


def test_map_antigravity_gemini_model_minimal_keyword_and_search_combo():
    """-minimal 关键词换算为家族内最接近档位；-search 组合保留档位信息。"""
    assert map_antigravity_gemini_model("gemini-3.8-flash-minimal", None, None) == "gemini-3.8-flash-low"
    assert map_antigravity_gemini_model("gemini-3.5-flash-minimal", None, None) == "gemini-3.5-flash-extra-low"
    assert map_antigravity_gemini_model("gemini-3.8-flash-high-search", None, None) == "gemini-3.8-flash-high"
    assert map_antigravity_gemini_model("假流式/gemini-3.8-flash-high", None, None) == "gemini-3.8-flash-high"
