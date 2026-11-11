"""Append configured system text without mutating the caller's nested objects."""

from typing import Any


async def append_configured_system_prompt(request: dict[str, Any]) -> None:
    from config import get_system_prompt_append

    text = await get_system_prompt_append()
    if not text.strip():
        return
    instruction = request.get("systemInstruction")
    if instruction is None:
        instruction = request.get("system_instructions")
    if isinstance(instruction, str):
        instruction = {"parts": [{"text": instruction}]}
    elif instruction is None:
        # 没有客户端 system prompt 时，A 不注入；保持请求原样。
        return
    if not isinstance(instruction, dict):
        return
    parts = instruction.get("parts") or []
    if not isinstance(parts, list):
        return
    if not parts:
        return
    # A+B 必须落在同一个文本 part 中；不改变客户端原有的后续 part。
    for index, part in enumerate(parts):
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            if part["text"].startswith(text):
                return
            parts = list(parts)
            parts[index] = {**part, "text": text + part["text"]}
            break
    else:
        parts = [{"text": text}, *parts]
    request["systemInstruction"] = {**instruction, "parts": parts}
    request.pop("system_instructions", None)
