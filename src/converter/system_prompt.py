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
        instruction = {}
    if not isinstance(instruction, dict):
        return
    parts = instruction.get("parts") or []
    if not isinstance(parts, list):
        return
    # Normalization may be repeated by retry/continuation paths.
    if not any(isinstance(part, dict) and part.get("text") == text for part in parts):
        parts = [*parts, {"text": text}]
    request["systemInstruction"] = {**instruction, "parts": parts}
    request.pop("system_instructions", None)
