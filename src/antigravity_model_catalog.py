"""Antigravity 模型目录静态基线 + 运行时热更新。

数据来源：2026-10-21 hub/2.25.0（CL 1003475615）sidecar 实抓
fetchAvailableModels 响应（见 docs/AGY-2.25.0-HOOK-封控对比-2026-10-21.md）。

用途：
1. labels.model_enum 必须填服务端下发的**枚举名**（MODEL_PLACEHOLDER_M318），
   而不是模型 ID（gemini-3.8-flash-high）——真实客户端如此，错格式可被服务端识别；
2. generationConfig 默认值按目录补齐（maxOutputTokens / thinkingConfig），
   对齐真实 agent 请求形状。

任何 fetchAvailableModels 响应流经时调用 update_from_catalog() 热更新，
上游新增/改名模型时无需改代码。
"""

from typing import Any, Dict, Optional

# model_id -> {enum, supports_thinking, thinking_budget, max_output_tokens}
# thinking_budget 为 None 表示目录未下发固定值（不主动补 thinkingConfig）。
_BASELINE: Dict[str, Dict[str, Any]] = {
    "chat_20706": {"enum": "MODEL_CHAT_20706", "supports_thinking": False, "thinking_budget": None, "max_output_tokens": None},
    "chat_23310": {"enum": "MODEL_CHAT_23310", "supports_thinking": False, "thinking_budget": None, "max_output_tokens": None},
    "claude-opus-4-6-thinking": {"enum": "MODEL_PLACEHOLDER_M26", "supports_thinking": True, "thinking_budget": 1024, "max_output_tokens": 64000},
    "claude-sonnet-4-6": {"enum": "MODEL_PLACEHOLDER_M35", "supports_thinking": True, "thinking_budget": 1024, "max_output_tokens": 64000},
    "gemini-2.5-flash": {"enum": "MODEL_GOOGLE_GEMINI_2_5_FLASH", "supports_thinking": True, "thinking_budget": None, "max_output_tokens": 65535},
    "gemini-2.5-flash-lite": {"enum": "MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE", "supports_thinking": True, "thinking_budget": None, "max_output_tokens": 65535},
    "gemini-2.5-flash-thinking": {"enum": "MODEL_GOOGLE_GEMINI_2_5_FLASH_THINKING", "supports_thinking": True, "thinking_budget": None, "max_output_tokens": 65535},
    "gemini-2.5-pro": {"enum": "MODEL_GOOGLE_GEMINI_2_5_PRO", "supports_thinking": True, "thinking_budget": 1024, "max_output_tokens": 65535},
    "gemini-3-flash": {"enum": "MODEL_PLACEHOLDER_M18", "supports_thinking": True, "thinking_budget": -1, "max_output_tokens": 65536},
    "gemini-3-flash-agent": {"enum": "MODEL_PLACEHOLDER_M84", "supports_thinking": True, "thinking_budget": -1, "max_output_tokens": 65536},
    "gemini-3.1-flash-image": {"enum": "MODEL_PLACEHOLDER_M21", "supports_thinking": False, "thinking_budget": None, "max_output_tokens": 65536},
    "gemini-3.1-flash-lite": {"enum": "MODEL_PLACEHOLDER_M50", "supports_thinking": True, "thinking_budget": None, "max_output_tokens": 65535},
    "gemini-3.5-flash-extra-low": {"enum": "MODEL_PLACEHOLDER_M187", "supports_thinking": True, "thinking_budget": 1000, "max_output_tokens": 65536},
    "gemini-3.5-flash-lite": {"enum": "MODEL_PLACEHOLDER_M198", "supports_thinking": True, "thinking_budget": None, "max_output_tokens": 65535},
    "gemini-3.5-flash-low": {"enum": "MODEL_PLACEHOLDER_M20", "supports_thinking": True, "thinking_budget": 4000, "max_output_tokens": 65536},
    "gemini-3.8-flash-high": {"enum": "MODEL_PLACEHOLDER_M318", "supports_thinking": True, "thinking_budget": -1, "max_output_tokens": 65536},
    "gemini-3.8-flash-low": {"enum": "MODEL_PLACEHOLDER_M320", "supports_thinking": True, "thinking_budget": 1000, "max_output_tokens": 65536},
    "gemini-3.8-flash-medium": {"enum": "MODEL_PLACEHOLDER_M319", "supports_thinking": True, "thinking_budget": 4000, "max_output_tokens": 65536},
    "gemini-3.8-flash-tiered": {"enum": "MODEL_PLACEHOLDER_M322", "supports_thinking": True, "thinking_budget": -1, "max_output_tokens": 65536},
    "gemini-4-argon-disabled": {"enum": "MODEL_PLACEHOLDER_M421", "supports_thinking": True, "thinking_budget": -1, "max_output_tokens": 65536},
    "gpt-oss-120b-medium": {"enum": "MODEL_OPENAI_GPT_OSS_120B_MEDIUM", "supports_thinking": True, "thinking_budget": 8192, "max_output_tokens": 32768},
    "tab_flash_lite_preview": {"enum": "MODEL_PLACEHOLDER_M19", "supports_thinking": False, "thinking_budget": None, "max_output_tokens": 4096},
    "tab_jump_flash_lite_preview": {"enum": "MODEL_PLACEHOLDER_M28", "supports_thinking": False, "thinking_budget": None, "max_output_tokens": 4096},
}

# 运行时目录 = 基线 + fetchAvailableModels 热更新
_catalog: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in _BASELINE.items()}


def update_from_catalog(models: Any) -> None:
    """从 fetchAvailableModels 响应的 models 字典热更新目录。

    只覆盖服务端实际下发的字段；未知模型自动收录。
    """
    if not isinstance(models, dict):
        return
    for model_id, info in models.items():
        if not isinstance(model_id, str) or not isinstance(info, dict):
            continue
        entry = _catalog.setdefault(model_id, {})
        enum = info.get("model")
        if isinstance(enum, str) and enum:
            entry["enum"] = enum
        if "supportsThinking" in info:
            entry["supports_thinking"] = bool(info.get("supportsThinking"))
        if info.get("thinkingBudget") is not None:
            try:
                entry["thinking_budget"] = int(info["thinkingBudget"])
            except (TypeError, ValueError):
                pass
        if info.get("maxOutputTokens") is not None:
            try:
                entry["max_output_tokens"] = int(info["maxOutputTokens"])
            except (TypeError, ValueError):
                pass


def get_model_enum(model_id: str) -> str:
    """返回模型对应的服务端枚举名；未知模型回退为模型 ID 本身。"""
    entry = _catalog.get(model_id)
    if entry and entry.get("enum"):
        return entry["enum"]
    return model_id


def get_model_info(model_id: str) -> Optional[Dict[str, Any]]:
    """返回目录条目（只读语义，勿原地修改）；未知模型返回 None。"""
    entry = _catalog.get(model_id)
    return dict(entry) if entry else None
