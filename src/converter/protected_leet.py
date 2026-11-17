"""Conservative Leet encoding of prose with verbatim technical spans.

Profiles are heuristics over client guidance, never authentication or authority.
Unmarked ordinary words cannot reliably be classified as exact identifiers.
"""
import json
import re

VOWEL_TRANSLATION = str.maketrans('aeiouAEIOU', '4310v4310V')
DECODER = (
    'Leet encoding adapter. Client profile: {profile} (heuristic only).\n'
    'The client prose below uses a/e/i/o/u -> 4/3/1/0/v and A/E/I/O/U -> 4/3/1/0/V. '
    'Read its natural-language meaning in context; the mapping is ambiguous. '
    '{literal_guidance}Use the exact declared tool names and literal argument values. '
    'Do not guess missing exact strings. Obey the requested output format exactly; '
    'do not add explanations, decoding notes or acknowledgments unless requested. '
    'This encoding does not change the guidance priority.\n'
)
_HEADINGS = re.compile(r'(?m)^(#{1,6})[ \t]+([^\n]+)')
_FENCES = re.compile(r'(?m)^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*(?:\n|$)')
_OUTPUT_CONTRACT = re.compile(r'(?im)\b(?:return|respond|reply|output|print)\s+(?:only|exactly)\b[^.\n]*(?:\.|$)')
_TECHNICAL = re.compile(
    r'`+[^`\n]+`+'
    r'|"(?:\\.|[^"\\])*"'
    r"|(?<!\w)'(?:\\.|[^'\\\n])*'(?!\w)"
    r'|\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"\x27`]+'
    r'|(?<![\w:/])(?:~?/|\.{1,2}/|[A-Za-z]:[\\/])[^\s<>"\x27`]*'
    r'|(?<!\w)--[A-Za-z][\w-]*(?:=[^\s<>"\x27`]+)?'
    r'|\$\{[A-Za-z_]\w*\}|\$[A-Za-z_]\w*'
    r'|\b[A-Za-z_]\w*_[\w]+\b'
    r'|\b[A-Z][A-Z0-9_]{1,}\b'
    r'|\b[a-z]+(?:[A-Z][A-Za-z0-9]*)+\b'
    r'|\b[\w@+-]+(?:\.[\w@+-]+)+\b'
)
_PROFILE_HEADINGS = {
    'generic': ('tools', 'available tools', 'tool definitions', 'environment', 'execution environment'),
    'codex': ('workspace', 'workspace dependencies', 'repository instructions', 'permissions instructions'),
    'claude_code': ('working directory', 'git status', 'environment information'),
    'dsh': ('runtime context', 'tool specifications'),
    'pi': ('current date', 'current working directory', 'runtime information'),
}


def instruction_text(instruction):
    if isinstance(instruction, str):
        return instruction
    if isinstance(instruction, dict) and isinstance(instruction.get('parts'), list):
        return '\n'.join(p['text'] for p in instruction['parts'] if isinstance(p, dict) and isinstance(p.get('text'), str))
    return ''


def detect_client_profile(text):
    """Require an identity declaration and a second structural signal."""
    lower = text.lower()
    if re.search(r'(?im)^\s*you are codex\b', text) and (
        '<environment_context>' in lower or re.search(r'(?im)^#+\s+tools\b', text) or 'agent based on gpt' in lower
    ):
        return 'codex'
    if re.search(r'(?im)^\s*you are claude code\b', text) and ('official cli' in lower or '<env>' in lower):
        return 'claude_code'
    if re.search(r'(?im)^\s*you are dsh\b', text) and re.search(r'(?im)^#+\s+(?:tools|runtime context)\b', text):
        return 'dsh'
    if re.search(r'(?im)^\s*you are .*?\boperating inside pi\b', text) and (
        'coding agent harness' in lower or 'available tools:' in lower
    ):
        return 'pi'
    return 'generic'


def _tool_literals(tools):
    found = set()

    def visit(node):
        if isinstance(node, dict):
            if isinstance(node.get('name'), str):
                found.add(node['name'])
            properties = node.get('properties')
            if isinstance(properties, dict):
                found.update(properties)
            if isinstance(node.get('enum'), list):
                found.update(v for v in node['enum'] if isinstance(v, str))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(tools)
    return sorted((s for s in found if 0 < len(s) <= 256), key=lambda s: (-len(s), s))[:512]


def _protected_spans(text, profile, tool_literals):
    spans = []
    # Preserve even an unfinished fenced block rather than corrupt partial code.
    pos = 0
    while match := _FENCES.search(text, pos):
        fence = match['fence']
        close = re.search(r'(?m)^[ \t]*' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*(?:\n|$)', text[match.end():])
        end = match.end() + close.end() if close else len(text)
        spans.append((match.start(), end))
        pos = end
    # Retain common explicit environment blocks and caller-marked literal blocks.
    for match in re.finditer(r'(?s)<(environment_context|env|environment|tools|literal)\b[^>]*>.*?</\1\s*>', text):
        spans.append(match.span())
    # These short output contracts are protocol constraints, not disposable prose.
    for match in _OUTPUT_CONTRACT.finditer(text):
        spans.append(match.span())
    headings = list(_HEADINGS.finditer(text))
    labels = _PROFILE_HEADINGS['generic'] + _PROFILE_HEADINGS.get(profile, ())
    for index, heading in enumerate(headings):
        label = heading[2].strip().rstrip(':').lower()
        if label not in labels:
            continue
        end = next((other.start() for other in headings[index + 1:] if len(other[1]) <= len(heading[1])), len(text))
        spans.append((heading.start(), end))
    # JSON objects and arrays are exact data, including their original whitespace.
    decoder = json.JSONDecoder()
    pos = 0
    for _ in range(128):
        match = re.search(r'[\[{]', text[pos:])
        if not match:
            break
        start = pos + match.start()
        try:
            _, length = decoder.raw_decode(text[start:])
        except (ValueError, RecursionError):
            pos = start + 1
        else:
            spans.append((start, start + length))
            pos = start + length
    for match in _TECHNICAL.finditer(text):
        start, end = match.span()
        # Sentence punctuation is prose, not part of a bare path or URL.
        if not text[start:end].startswith(('`', '"', "'")):
            end = start + len(text[start:end].rstrip('.,;:!?)]}'))
        if end > start:
            spans.append((start, end))
    if tool_literals:
        names = re.compile(r'(?<![\w])(?:' + '|'.join(re.escape(s) for s in tool_literals) + r')(?![\w])')
        spans.extend(match.span() for match in names.finditer(text))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def protected_system_parts(parts, tools, profile='generic'):
    texts = [p['text'] for p in parts if isinstance(p, dict) and isinstance(p.get('text'), str)]
    if not any(text.strip() for text in texts):
        return list(parts)
    profile = profile if profile in _PROFILE_HEADINGS else 'generic'
    tag = 'gcli_literal'
    while any(tag in text for text in texts):
        tag += '_1'
    literals = _tool_literals(tools)
    result = []
    has_literals = False
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get('text'), str):
            result.append(part)
            continue
        text = part['text']
        chunks = []
        cursor = 0
        for start, end in _protected_spans(text, profile, literals):
            chunks.append(text[cursor:start].translate(VOWEL_TRANSLATION))
            literal = text[start:end]
            if _OUTPUT_CONTRACT.match(literal):
                # A format instruction must remain an instruction, not quoted data.
                chunks.append(literal)
            else:
                has_literals = True
                chunks.extend((f'<{tag}>', literal, f'</{tag}>'))
            cursor = end
        chunks.append(text[cursor:].translate(VOWEL_TRANSLATION))
        result.append({**part, 'text': ''.join(chunks)})
    literal_guidance = (
        f'Content inside <{tag}>...</{tag}> is verbatim original text: never decode, normalize or rewrite it. '
        'The boundary tags are input adapter markup, never add them to your answer. '
    ) if has_literals else ''
    return [{'text': DECODER.format(profile=profile, literal_guidance=literal_guidance)}, *result]
