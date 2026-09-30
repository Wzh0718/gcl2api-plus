"""
敏感词替换模块

在请求进入上游（GeminiCLI / Antigravity）之前，按用户配置的规则替换
system prompt（systemInstruction）中的敏感词，避免上游对客户端指纹
（如 "Claude Code" 等字样）做特征匹配触发伪 429。

规则格式（每行一条）：
    查找词=>替换词      命中后替换为指定内容
    查找词              无 "=>" 时替换为空（即删除该词）
    # 注释             以 "#" 开头的行会被忽略

匹配方式为区分大小写的子串替换；仅处理 system prompt，不影响消息正文。
"""

from typing import Any, Dict, List, Tuple

from log import log

RULE_SEPARATOR = "=>"
MAX_RULE_COUNT = 200


def parse_sensitive_word_rules(rules_text: Any) -> List[Tuple[str, str]]:
    """解析规则文本为 (查找词, 替换词) 列表。

    空行、注释行（# 开头）被忽略；查找词与替换词两端空白会被去除；
    查找词为空的行被丢弃；最多返回 MAX_RULE_COUNT 条。
    """
    if not isinstance(rules_text, str) or not rules_text.strip():
        return []

    rules: List[Tuple[str, str]] = []
    for raw_line in rules_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if RULE_SEPARATOR in line:
            find, replace = line.split(RULE_SEPARATOR, 1)
            find, replace = find.strip(), replace.strip()
        else:
            find, replace = line, ""
        if not find:
            continue
        rules.append((find, replace))
        if len(rules) >= MAX_RULE_COUNT:
            break
    return rules


def _replace_text(text: str, rules: List[Tuple[str, str]]) -> Tuple[str, int]:
    """对单段文本应用规则，返回 (新文本, 命中次数)。"""
    hits = 0
    for find, replace in rules:
        if find and find in text:
            hits += text.count(find)
            text = text.replace(find, replace)
    return text, hits


def _set_instruction(request_data: Dict[str, Any], instruction: Dict[str, Any]) -> None:
    """按请求中实际使用的键写回 systemInstruction。"""
    if "systemInstruction" in request_data:
        request_data["systemInstruction"] = instruction
    else:
        request_data["system_instructions"] = instruction


def apply_sensitive_word_rules(
    request_data: Dict[str, Any], rules: List[Tuple[str, str]]
) -> int:
    """对请求的 system prompt 应用敏感词替换。

    采用写时复制：只有命中替换时才会重建 parts / instruction 对象，
    不会改动调用方原始嵌套结构。

    Returns:
        命中的替换次数。
    """
    if not rules or not isinstance(request_data, dict):
        return 0

    instruction = request_data.get("systemInstruction")
    if instruction is None:
        instruction = request_data.get("system_instructions")
    if instruction is None:
        return 0

    # systemInstruction 直接是字符串的防御性处理
    if isinstance(instruction, str):
        new_text, hits = _replace_text(instruction, rules)
        if hits:
            _set_instruction(request_data, {"parts": [{"text": new_text}]})
        return hits

    if not isinstance(instruction, dict):
        return 0

    parts = instruction.get("parts")
    if not isinstance(parts, list):
        return 0

    total_hits = 0
    new_parts = list(parts)
    changed = False
    for index, part in enumerate(parts):
        if isinstance(part, str):
            new_text, hits = _replace_text(part, rules)
            if hits:
                new_parts[index] = {"text": new_text}
                total_hits += hits
                changed = True
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            new_text, hits = _replace_text(part["text"], rules)
            if hits:
                new_part = dict(part)
                new_part["text"] = new_text
                new_parts[index] = new_part
                total_hits += hits
                changed = True

    if changed:
        new_instruction = dict(instruction)
        new_instruction["parts"] = new_parts
        _set_instruction(request_data, new_instruction)

    return total_hits


async def filter_request_system_instruction(
    request_data: Dict[str, Any], *, mode: str = ""
) -> None:
    """按当前配置对请求的 system prompt 执行敏感词替换（原地更新 request_data）。

    未启用、规则为空或替换失败时均不影响原请求。
    """
    try:
        from config import (
            get_sensitive_word_replace_enabled,
            get_sensitive_word_replace_rules,
        )

        if not await get_sensitive_word_replace_enabled():
            return
        rules = parse_sensitive_word_rules(await get_sensitive_word_replace_rules())
        if not rules:
            return
        hits = apply_sensitive_word_rules(request_data, rules)
        if hits:
            log.info(
                f"[SENSITIVE_WORDS] mode={mode or 'unknown'} "
                f"已在 system prompt 中替换 {hits} 处敏感词"
            )
    except Exception as e:
        # 替换失败不应阻断正常请求
        log.warning(f"[SENSITIVE_WORDS] 敏感词替换失败，已跳过: {e}")
