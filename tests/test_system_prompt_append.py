from copy import deepcopy

import config
import pytest

from src.converter.gemini_fix import normalize_gemini_request


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {"sensitive_word_replace_enabled": False})
    monkeypatch.delenv("SYSTEM_PROMPT_APPEND", raising=False)


async def test_append_priority_and_empty_override(monkeypatch):
    assert await config.get_system_prompt_append() == ""
    monkeypatch.setenv("SYSTEM_PROMPT_APPEND", "env text")
    assert await config.get_system_prompt_append() == "env text"
    config._config_cache["system_prompt_append"] = "sqlite text"
    assert await config.get_system_prompt_append() == "sqlite text"
    config._config_cache["system_prompt_append"] = ""
    assert await config.get_system_prompt_append() == ""


@pytest.mark.parametrize("mode", ["antigravity", "geminicli", "vertex"])
async def test_append_preserves_original_and_does_not_duplicate(mode):
    config._config_cache["system_prompt_append"] = "custom text\nsecond line"
    req = {"model": "gemini-2.5-flash", "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
           "systemInstruction": {"role": "system", "parts": [{"text": "original"}]}}
    original = deepcopy(req)
    result = await normalize_gemini_request(req, mode=mode)
    assert req == original
    assert result["systemInstruction"]["parts"] == [{"text": "custom text\nsecond lineoriginal"}]
    second = await normalize_gemini_request(result, mode=mode)
    assert second["systemInstruction"]["parts"] == result["systemInstruction"]["parts"]


async def test_no_existing_system_and_empty_setting():
    req = {"model": "gemini-2.5-flash", "contents": []}
    result = await normalize_gemini_request(req)
    assert "systemInstruction" not in result
    config._config_cache["system_prompt_append"] = "new instruction"
    result = await normalize_gemini_request(req)
    assert "systemInstruction" not in result


@pytest.mark.parametrize("instruction", ["original", {"parts": [{"text": "original"}]}])
async def test_legacy_instruction_shape(instruction):
    config._config_cache["system_prompt_append"] = "append"
    result = await normalize_gemini_request({"model": "gemini-2.5-flash", "contents": [], "system_instructions": instruction})
    assert result["systemInstruction"]["parts"][0] == {"text": "appendoriginal"}
