"""从调用监控的日志详情里审计网关引导词（BRIDGE）的实际注入情况。

数据来源是 `call_records.request_details.upstream`（内容捕获开启时写入）：
`{system, messages, tools, parameters}` 与可选的 `response`。这里只读日志，
不发起任何上游请求。

审计分两层：

1. 注入层（有 system 就能判）：基线版本、引导词是否存在/唯一/位于末尾、
   运行时块是否泄漏、工具声明清单。
2. 依从层（有 response 才能判）：模型是否调用了未声明的工具、是否编造了
   请求里不存在的绝对路径、是否声称执行过没有执行的动作。

依从层是启发式，只用于抽样巡检；命中不等于违规，需要人工看上下文。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

NEW_BRIDGE_MARKER = '=== END OF HOST DOCUMENT ==='
LEGACY_BRIDGE_MARKER = 'Execution environment adapter'
RUNTIME_BLOCK_MARKER = '<user_information>'

# 2.35 起基线带完整段落；2.31 及更早只有三个基础模块。
_BASELINE_235_MARKERS = ('<identity>', '<work_efficiently>')
_LEGACY_BASELINE_PREFIX = 'You are Antigravity'

_ABSOLUTE_PATH_RE = re.compile(r'(?<![\w:/])/(?:home|Users|root|var|opt|srv|mnt)/[A-Za-z0-9._/-]{2,}')
_EXECUTION_CLAIM_RE = re.compile(
    r'\b(I (?:ran|executed|created|wrote|read) )|(已执行)|(命令输出)|(执行结果)',
    re.IGNORECASE,
)


def _system_parts(details: Dict[str, Any]) -> List[Dict[str, Any]]:
    upstream = details.get('upstream') or {}
    instruction = upstream.get('system') or {}
    if isinstance(instruction, dict):
        instruction = instruction.get('systemInstruction') or instruction.get('system_instructions') or instruction
    if not isinstance(instruction, dict):
        return []
    parts = instruction.get('parts')
    return [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []


def extract_system_text(details: Dict[str, Any]) -> str:
    """拼接 systemInstruction 的文本 part（保持顺序）。"""
    return '\n'.join(
        part.get('text', '') for part in _system_parts(details) if isinstance(part.get('text'), str)
    )


def extract_response_text(details: Dict[str, Any]) -> str:
    """尽力取到模型响应文本；没有捕获时返回空串。"""
    candidates: List[Any] = []
    upstream = details.get('upstream') or {}
    if isinstance(upstream, dict):
        candidates.extend([upstream.get('response'), upstream.get('downstream')])
    candidates.append(details.get('downstream'))
    candidates.append(details.get('response'))

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        if isinstance(candidate, dict):
            text = _response_text_from_payload(candidate)
            if text:
                return text
    return ''


def _response_text_from_payload(payload: Dict[str, Any]) -> str:
    chunks: List[str] = []
    candidates = payload.get('candidates')
    if isinstance(candidates, list):
        for candidate in candidates:
            content = candidate.get('content') if isinstance(candidate, dict) else None
            parts = content.get('parts') if isinstance(content, dict) else None
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, dict) and isinstance(part.get('text'), str):
                        chunks.append(part['text'])
    if not chunks and isinstance(payload.get('text'), str):
        chunks.append(payload['text'])
    return '\n'.join(chunks)


def declared_tool_names(details: Dict[str, Any]) -> List[str]:
    upstream = details.get('upstream') or {}
    tools = upstream.get('tools') if isinstance(upstream, dict) else None
    names: List[str] = []
    if isinstance(tools, list):
        for entry in tools:
            if not isinstance(entry, dict):
                continue
            for declaration in entry.get('functionDeclarations') or []:
                if isinstance(declaration, dict) and isinstance(declaration.get('name'), str):
                    names.append(declaration['name'])
    return names


def response_function_calls(details: Dict[str, Any]) -> List[str]:
    upstream = details.get('upstream') or {}
    payload = upstream.get('response') if isinstance(upstream, dict) else None
    names: List[str] = []
    if isinstance(payload, dict):
        for candidate in payload.get('candidates') or []:
            content = candidate.get('content') if isinstance(candidate, dict) else None
            for part in (content or {}).get('parts') or []:
                call = part.get('functionCall') if isinstance(part, dict) else None
                if isinstance(call, dict) and isinstance(call.get('name'), str):
                    names.append(call['name'])
    return names


def detect_baseline_version(text: str) -> str:
    """按段落特征判断基线版本；无法判断返回 'unknown'。"""
    if not text:
        return 'absent'
    if all(marker in text for marker in _BASELINE_235_MARKERS):
        return '2.35.x'
    if _LEGACY_BASELINE_PREFIX in text and '<work_efficiently>' not in text:
        return '<=2.31.x'
    return 'unknown'


def is_image_model(model: Optional[str]) -> bool:
    """图片模型的原生信封不带 system，缺失不算问题。"""
    return bool(model) and 'image' in str(model).lower()


def audit_details(details: Dict[str, Any], model: Optional[str] = None) -> Dict[str, Any]:
    """审一条日志详情，返回可断言的结论与问题码。"""
    text = extract_system_text(details)
    tools = declared_tool_names(details)
    response = extract_response_text(details)
    calls = response_function_calls(details)

    new_count = text.count(NEW_BRIDGE_MARKER)
    legacy_count = text.count(LEGACY_BRIDGE_MARKER)
    problems: List[str] = []
    notes: List[str] = []

    if not text:
        if is_image_model(model):
            notes.append('system_not_expected')
        else:
            problems.append('system_absent')
    else:
        if new_count == 0:
            problems.append('bridge_missing' if legacy_count == 0 else 'bridge_legacy')
        if new_count > 1:
            problems.append('bridge_duplicated')
        if new_count == 1:
            rest = _text_after_bridge(text)
            if rest is None:
                problems.append('bridge_text_mismatch')
            elif rest and rest != _structured_tail():
                problems.append('bridge_not_at_tail')
        if RUNTIME_BLOCK_MARKER in text:
            problems.append('runtime_block_leaked')

    baseline = detect_baseline_version(text)

    undeclared = sorted({name for name in calls if name not in set(tools)})
    if undeclared:
        problems.append('undeclared_tool_call')
    if response:
        if _EXECUTION_CLAIM_RE.search(response):
            notes.append('execution_claim')
        invented = _invented_paths(response, text)
        if invented:
            notes.append('possible_invented_path:' + ','.join(invented[:3]))
    else:
        notes.append('response_unavailable')

    return {
        'baseline': baseline,
        'bridge_new': new_count,
        'bridge_legacy': legacy_count,
        'tools': tools,
        'response_chars': len(response),
        'problems': problems,
        'notes': notes,
    }


def bridge_last_line() -> str:
    """当前引导词的最后一行。"""
    from src.converter.antigravity_system_prompt import BRIDGE

    return BRIDGE.strip().splitlines()[-1].strip()


def _text_after_bridge(text: str):
    """引导词之后的剩余文本；没有当前引导词原文时返回 None。"""
    from src.converter.antigravity_system_prompt import BRIDGE

    if BRIDGE not in text:
        return None
    return text.split(BRIDGE, 1)[1].strip()


def _structured_tail() -> str:
    from src.converter.antigravity_system_prompt import STRUCTURED_OUTPUT_PRIORITY

    return STRUCTURED_OUTPUT_PRIORITY.strip()


def _invented_paths(response: str, system_text: str) -> List[str]:
    found = {match.group(0) for match in _ABSOLUTE_PATH_RE.finditer(response)}
    return sorted(path for path in found if path not in system_text)


def iter_records(db, limit: int = 20) -> Iterable[Any]:
    """按 id 倒序取最近 limit 条带详情的记录（兼容 sqlite3.Row / dict）。"""
    cursor = db.execute(
        'SELECT id, channel, model_name, request_details FROM call_records '
        "WHERE length(request_details) > 2 ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return cursor.fetchall()


def summarize(reports: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {'records': len(reports), 'ok': 0}
    for report in reports:
        for problem in report['audit']['problems']:
            summary[problem] = summary.get(problem, 0) + 1
        if not report['audit']['problems']:
            summary['ok'] += 1
    return summary


def render_table(reports: List[Dict[str, Any]]) -> str:
    lines = ['id\tmodel\tbaseline\tnew/legacy\tproblems\tnotes']
    for report in reports:
        audit = report['audit']
        lines.append('\t'.join([
            str(report['id']),
            str(report.get('model') or ''),
            audit['baseline'],
            f"{audit['bridge_new']}/{audit['bridge_legacy']}",
            ','.join(audit['problems']) or '-',
            ','.join(audit['notes']) or '-',
        ]))
    return '\n'.join(lines)


def dumps(details: Any) -> Any:
    return json.loads(details) if isinstance(details, (str, bytes)) else details
