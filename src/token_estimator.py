"""简单的 token 估算，不追求精确"""
from __future__ import annotations

from typing import Any, Dict


_IMAGE_TOKENS = 300


def estimate_gemini_input_tokens(payload: Dict[str, Any]) -> int:
    """Estimate Gemini text and image parts without fetching remote media."""
    if not isinstance(payload, dict):
        return 0
    request = payload.get("generateContentRequest", payload)
    if not isinstance(request, dict):
        return 0
    total = 0
    for content in request.get("contents") or []:
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                total += max(1, len(text) // 4)
            media = (
                part.get("inlineData") or part.get("inline_data")
                or part.get("fileData") or part.get("file_data")
            )
            if isinstance(media, dict):
                mime_type = media.get("mimeType") or media.get("mime_type")
                if isinstance(mime_type, str) and mime_type.lower().startswith("image/"):
                    total += _IMAGE_TOKENS
    return total


def estimate_input_tokens(payload: Dict[str, Any]) -> int:
    """粗略估算 token 数：字符数 / 4 + 图片固定值"""
    total_chars = 0
    image_count = 0

    # 统计所有文本字符
    def count_str(obj: Any) -> None:
        nonlocal total_chars, image_count
        if isinstance(obj, str):
            total_chars += len(obj)
        elif isinstance(obj, dict):
            # 检测图片
            if obj.get("type") == "image" or "inlineData" in obj:
                image_count += 1
                return
            for v in obj.values():
                count_str(v)
        elif isinstance(obj, list):
            for item in obj:
                count_str(item)

    count_str(payload)

    # 粗略估算：字符数/4 + 每张图片300 tokens
    return max(1, total_chars // 4 + image_count * _IMAGE_TOKENS)
