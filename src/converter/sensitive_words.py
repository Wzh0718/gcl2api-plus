"""系统提示中的客户端名称过滤。

支持 rewrite / block，不区分大小写、长词优先；仅改系统提示。
自定义规则格式为 查找词=>替换词，无 => 时删除；# 开头为注释。
"""

import re
from typing import Any, Dict, List, Tuple

from fastapi import HTTPException

from log import log

RULE_SEPARATOR = "=>"
MAX_RULE_COUNT = 200

# 客户端名称预设；自定义同名规则（不区分大小写）优先。
DEFAULT_CLIENT_KEYWORDS = (
    "Claude Code", "Claude Agent SDK", "OpenAI Codex", "Codex CLI", "Codex",
    "OpenCode", "GitHub Copilot CLI", "GitHub Copilot", "Copilot CLI",
    "Gemini Code Assist", "Gemini CLI", "Cursor", "Windsurf", "Codeium",
    "Cline", "Roo Code", "Kilo Code", "Aider", "Continue.dev",
    "Amazon Q Developer", "Amazon CodeWhisperer", "CodeWhisperer",
    "JetBrains AI Assistant", "JetBrains AI", "JetBrains Junie", "Junie",
    "Kiro", "Qoder CLI", "Qoder", "Qwen Code", "Trae", "Tabnine",
    "Sourcegraph Cody", "Augment Code", "Replit Agent", "Ghostwriter",
    "Devin", "OpenHands", "SWE-agent", "Goose", "Zed AI", "Void Editor",
    "PearAI", "Refact.ai", "Tabby", "GitLab Duo", "Visual Studio IntelliCode",
    "CodeBuddy", "Blackbox AI", "Pieces for Developers", "Qodo", "CodiumAI",
    "Rovo Dev CLI", "Factory Droid", "OpenClaw", "Clawdbot", "Moltbot",
    "Hermes Agent", "Hermes", "WorkBuddy",
)


def effective_rules(custom_rules: List[Tuple[str, str]], use_defaults: bool) -> List[Tuple[str, str]]:
    rules = [(name, "Antigravity") for name in DEFAULT_CLIENT_KEYWORDS] if use_defaults else []
    rules.extend(custom_rules)
    unique = {name.casefold(): (name, replacement) for name, replacement in rules if name}
    return sorted(unique.values(), key=lambda rule: -len(rule[0]))


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
    rules = effective_rules(rules, False)
    if not rules:
        return text, 0
    # 单次替换：长词先匹配，替换结果不会再次被其他规则修改。
    patterns = [re.compile(re.escape(name), re.IGNORECASE) for name, _ in rules]
    pattern = re.compile("|".join(re.escape(name) for name, _ in rules), re.IGNORECASE)

    def replace(match):
        return next(value for regex, (_, value) in zip(patterns, rules) if regex.fullmatch(match.group()))

    return pattern.subn(replace, text)


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


async def filter_compatibility_system_prompts(request_data: Dict[str, Any], *, mode: str) -> None:
    """兼容模式将系统提示改成 user 前执行过滤，保留真正的用户消息。"""
    from config import get_compatibility_mode_enabled

    if not await get_compatibility_mode_enabled():
        return

    async def filtered(content):
        parts = [{"text": content}] if isinstance(content, str) else content
        if not isinstance(parts, list):
            return content
        wrapped = {"systemInstruction": {"parts": parts}}
        await filter_request_system_instruction(wrapped, mode=mode)
        result = wrapped["systemInstruction"]["parts"]
        return result[0]["text"] if isinstance(content, str) else result

    if "system" in request_data:
        request_data["system"] = await filtered(request_data["system"])
    messages = []
    for message in request_data.get("messages", []):
        if message.get("role") == "system":
            message = dict(message, content=await filtered(message.get("content")))
        messages.append(message)
    request_data["messages"] = messages


async def filter_request_system_instruction(
    request_data: Dict[str, Any], *, mode: str = "", block_only: bool = False
) -> None:
    """按当前配置对请求的 system prompt 执行敏感词替换（原地更新 request_data）。

    未启用或有效规则为空时放行；block 命中抛出本地 HTTP 403。
    """
    try:
        from config import (
            get_sensitive_word_replace_enabled,
            get_sensitive_word_replace_rules,
            get_sensitive_word_filter_mode,
            get_sensitive_word_use_default_keywords,
        )

        if not await get_sensitive_word_replace_enabled():
            return
        filter_mode = await get_sensitive_word_filter_mode()
        if block_only and filter_mode != "block":
            return
        rules = effective_rules(
            parse_sensitive_word_rules(await get_sensitive_word_replace_rules()),
            await get_sensitive_word_use_default_keywords(),
        )
        if not rules:
            return
        target = request_data.copy() if filter_mode == "block" else request_data
        hits = apply_sensitive_word_rules(target, rules)
        if hits and filter_mode == "block":
            raise HTTPException(status_code=403, detail={
                "code": "blocked_by_antigravity_coding_filter",
                "type": "invalid_request_error",
                "message": "System prompt matches a configured coding-client keyword",
            })
        if hits:
            log.info(
                f"[SENSITIVE_WORDS] mode={mode or 'unknown'} "
                f"已在 system prompt 中替换 {hits} 处敏感词"
            )
    except HTTPException:
        raise
    except Exception as e:
        # 替换失败不应阻断正常请求
        log.warning(f"[SENSITIVE_WORDS] 敏感词替换失败，已跳过: {e}")
