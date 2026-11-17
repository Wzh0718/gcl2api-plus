"""两条文本入口的转换产物形态必须收敛。

OpenAI 与 Anthropic 入口最终都汇入 `wrap_cli_request`，因此同一语义的请求
不应该因为入口协议不同而产生不同的下游形态。本测试锁定这一点，避免某个入口
再次悄悄注入调用方没有表达过的字段。

已知且有意保留的差异收在 `ALLOWED_DIVERGENCE` 里，需要单独的产品决定才能调整。
"""

import asyncio
import copy

import pytest

from src.converter.anthropic2gemini import anthropic_to_gemini_request
from src.converter.openai2gemini import convert_openai_to_gemini_request

MODEL = "gemini-3-pro-preview"
SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}

# 采样参数的默认值属于产品参数，当前两条入口口径不同，收敛需要单独拍板。
ALLOWED_DIVERGENCE = {"generationConfig.temperature", "generationConfig.topP"}


def openai_request(with_params=True):
    request = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "d",
                    "parameters": copy.deepcopy(SCHEMA),
                },
            }
        ],
        "tool_choice": "auto",
    }
    if with_params:
        request.update({"max_tokens": 1024, "temperature": 0.7})
    return request


def anthropic_request(with_params=True):
    request = {
        "model": MODEL,
        "system": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "get_weather",
                "description": "d",
                "input_schema": copy.deepcopy(SCHEMA),
            }
        ],
        "tool_choice": {"type": "auto"},
    }
    if with_params:
        request.update({"max_tokens": 1024, "temperature": 0.7})
    return request


def shape(value, prefix=""):
    """展开为可比较的字段路径集合。"""
    paths = []
    if isinstance(value, dict):
        for key in sorted(value):
            paths.append(prefix + key)
            paths.extend(shape(value[key], prefix + key + "."))
    elif isinstance(value, list) and value:
        paths.extend(shape(value[0], prefix + "[]."))
    return paths


async def convert_both(with_params):
    openai = await convert_openai_to_gemini_request(openai_request(with_params))
    anthropic = await anthropic_to_gemini_request(anthropic_request(with_params))
    return openai, anthropic


@pytest.mark.asyncio
@pytest.mark.parametrize("with_params", [True, False])
async def test_entry_parity_has_no_unintended_fields(with_params):
    """除已知采样差异外，两条入口不得产生独占字段。"""
    openai, anthropic = await convert_both(with_params)
    only_openai = set(shape(openai)) - set(shape(anthropic))
    only_anthropic = set(shape(anthropic)) - set(shape(openai))

    assert only_openai == set()
    assert only_anthropic <= ALLOWED_DIVERGENCE, (
        f"Anthropic 入口出现了未收敛的独占字段：{sorted(only_anthropic - ALLOWED_DIVERGENCE)}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_params", [True, False])
async def test_no_injected_stop_sequences_or_candidate_count(with_params):
    """不注入调用方没有指定的停止符与候选数。"""
    openai, anthropic = await convert_both(with_params)
    for config in (openai.get("generationConfig") or {}, anthropic.get("generationConfig") or {}):
        assert "stopSequences" not in config
        assert "candidateCount" not in config


@pytest.mark.asyncio
async def test_tools_and_system_instruction_are_identical():
    """工具与系统指令的形态必须逐字一致。"""
    openai, anthropic = await convert_both(True)
    assert openai["tools"] == anthropic["tools"]
    assert openai["systemInstruction"] == anthropic["systemInstruction"]
    assert openai["toolConfig"] == anthropic["toolConfig"]


@pytest.mark.asyncio
async def test_caller_stop_sequences_still_pass_through():
    """调用方显式指定的 stop 必须保留（只是不再有硬编码默认值）。"""
    request = anthropic_request(True)
    request["stop_sequences"] = ["END"]
    converted = await anthropic_to_gemini_request(request)
    assert converted["generationConfig"].get("stopSequences") == ["END"]
