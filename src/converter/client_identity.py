"""客户端 harness 身份归一化。

上游按 system 文本识别调用方。第三方 harness 的固定身份句与专有结构标题
（DSH 的 ``You are an AI agent powered by DeepSeek Harness.`` 与
``# Runtime Context`` / ``# Tool Specifications``）会和注入的官方基线拼成
官方客户端从不会产生的形态：一整段 Antigravity 官方文档后面，紧跟一句
另一家产品的自我声明。

这里只改两类东西：

1. **身份开场句**。任何 ``You are [an] [AI] agent powered by <X>.`` 都换成
   中性 persona，去掉品牌声明；
2. **harness 专有标题**。DSH 的 ``# Runtime Context`` / ``# Tool Specifications``
   换成通用标题。

业务正文、工具说明、文件路径、输出约定一律不动，因此不影响指令遵循。
未命中时返回原对象，调用方可以据此判断是否需要写回。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List

# 只匹配 part 开头：身份句永远在最前面，正文里引用同一句式不应被改。
_IDENTITY_OPENER = re.compile(
    r'\A(?P<lead>[ \t]*)'
    r'You are (?:an? )?(?:AI )?(?:agent )?powered by '
    r'[A-Za-z0-9][A-Za-z0-9 ._\-]{0,60}?'
    r'\.[ \t]*$',
    re.IGNORECASE | re.MULTILINE,
)

NEUTRAL_PERSONA = 'You are a coding assistant.'

# ``You are a coding assistant operating inside DeepSeek Harness.`` 这类 persona
# 声明同样带品牌，而且官方文档里从不出现该句式。
_HOST_APP = re.compile(
    r'\boperating (?:inside|within) '
    r'(?P<target>[A-Z][A-Za-z0-9_\-]*(?: [A-Z][A-Za-z0-9_\-]*){0,3})'
    r'(?=[.,;:!\n]|$)'
)

# 顺序敏感：先长后短，避免前缀互相吃掉。
_HARNESS_HEADINGS = (
    (re.compile(r'(?m)^#+[ \t]+Runtime Context[ \t]*$'), '# Environment'),
    (re.compile(r'(?m)^#+[ \t]+Tool Specifications[ \t]*$'), '# Tools'),
)


def _normalize_text(text: str) -> str | None:
    """返回归一化后的文本；没有命中时返回 None。"""
    if not text:
        return None
    opener = _IDENTITY_OPENER.match(text)
    if opener is None:
        return None
    replaced = text[: opener.start()] + opener['lead'] + NEUTRAL_PERSONA + text[opener.end():]
    replaced = _HOST_APP.sub('operating inside the host application', replaced)
    for pattern, replacement in _HARNESS_HEADINGS:
        replaced = pattern.sub(replacement, replaced)
    return replaced


def normalize_client_identity(parts: Iterable[Any]) -> List[Any]:
    """对 system parts 做身份归一化，未命中时返回原序列内容。

    只处理 ``{"text": ...}`` 形态的 part；其它 part（文件、图片、结构化块）
    原样保留，命中数量为零时不重建列表，保证幂等。
    """
    items = list(parts)
    changed = False
    result: List[Any] = []
    for part in items:
        if isinstance(part, dict) and isinstance(part.get('text'), str):
            text = _normalize_text(part['text'])
            if text is not None and text != part['text']:
                result.append({**part, 'text': text})
                changed = True
                continue
        result.append(part)
    return result if changed else items


def identity_hits(parts: Iterable[Any]) -> int:
    """命中条数，用于日志与诊断，不改变输入。"""
    return sum(
        1
        for part in parts
        if isinstance(part, dict) and isinstance(part.get('text'), str)
        and _normalize_text(part['text']) is not None
    )
