"""Conservative Leet encoding of prose with verbatim technical spans.

Profiles are heuristics over client guidance, never authentication or authority.
Unmarked ordinary words cannot reliably be classified as exact identifiers.
"""
import re
from bisect import bisect_right

VOWEL_TRANSLATION = str.maketrans('aeiouAEIOU', '4310v4310V')
_PARAGRAPH_BREAK = re.compile(r'\r?\n[ \t]*\r?\n')
DECODER = (
    'Leet encoding adapter. Client profile: {profile} (heuristic only).\n'
    'The client prose below uses a/e/i/o/u -> 4/3/1/0/v and A/E/I/O/U -> 4/3/1/0/V. '
    'Read the encoded prose in context; the mapping is ambiguous. '
    'Do not apply it to user messages or to your response. Use exact output strings and '
    'response schema field names and enum values as provided. '
    '{literal_guidance}Use the exact declared tool names and literal argument values. '
    'Do not guess missing exact strings. Obey the requested output format exactly; '
    'do not add explanations, decoding notes or acknowledgments unless requested. '
    'This encoding does not change the guidance priority.\n'
)
_HEADINGS = re.compile(r'(?m)^(#{1,6})[ \t]+([^\r\n]+)')
_FENCES = re.compile(r'(?m)^[ \t]*(?P<fence>`{3,}|~{3,})[^\r\n]*(?:\r?\n|$)')
_LITERAL_BLOCK = re.compile(r'<literal\b[^>]*>.*?</literal\s*>', re.DOTALL)
_TECHNICAL_BLOCK = re.compile(
    r'<(environment_context|env|environment|tools|(?:response|output|json)[_-]schema)\b[^>]*>.*?</\1\s*>',
    re.DOTALL | re.IGNORECASE,
)
_OUTPUT_CONTRACT = re.compile(
    r'(?im)\b(?:return|respond|reply|answer|output|print)\s+(?:with\s+)?'
    r'(?:only|exactly)\b[^\n]*?(?:\.(?=\s|$)|$)'
)
_QUOTED = (
    r'"(?:\\.|[^"\\\r\n])*"'
    r"|(?<!\w)'(?:\\.|[^'\\\r\n])*'(?!\w)"
    r'|“[^”\r\n]*”|‘[^’\r\n]*’|「[^」\r\n]*」|『[^』\r\n]*』'
    r'|`+[^`\r\n]+`+'
)
_VERBATIM = re.compile(
    _QUOTED
    + r'|(?a:\b(?:true|false|null|True|False|None)\b)'
    r'|(?a:\b(?:[A-Z]/)+[A-Z]\b)'
    # Bare protocol markers such as FINAL_JSON_START are matched verbatim by callers.
    r'|(?a:\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b)'
)
_OUTPUT_TITLE = (
    r'(?:(?:output|response|return)(?:[ \t]+(?:format|contract|schema|structure|template|requirements|rules|fields|examples?))?'
    r'|json[ \t]+(?:schema|output)'
    r'|structured[ \t]+(?:output|response)(?:[ \t]+(?:compatibility|format|schema))?'
    r'|(?:输出|返回|响应|回复)(?:格式|约定|结构|模板|要求|规则|字段|示例|样例|[ \t]*schema)?'
    r'|结构化(?:输出|返回|响应)(?:格式|约定|结构|模板)?)'
)
_OUTPUT_HEADING = re.compile(_OUTPUT_TITLE, re.IGNORECASE)
_FORMAT_NAME = r'(?:JSONL?|NDJSON|YAML|XML|CSV|TSV|TOML)'
_OUTPUT_COMMAND = (
    r'\b(?:(?:return|respond|reply|output|emit|produce|generate)[ \t]++'
    r'(?:[^.!?;\r\n]{0,120}?\b(?:as|in|with)[ \t]++)?|(?:the|your)[ \t]+(?:final[ \t]+)?'
    r'(?:response|output|result)[ \t]+(?:must|should)[ \t]+be[ \t]+)'
    r'(?:(?:only|exactly)[ \t]+)?(?:(?:a|an|the)[ \t]+)?'
    rf'(?:(?:valid|strict|pure|raw|parseable)[ \t]+)?[`"\']?{_FORMAT_NAME}(?![A-Za-z0-9_])'
    r'|(?:输出|返回|回复|以)[ \t]*(?:仅|只)?[ \t]*(?:一个|一份|一条)?[ \t]*'
    rf'(?:有效的?|纯|严格的?|可解析的?)?[ \t]*[`"\']?{_FORMAT_NAME}(?![A-Za-z0-9_])'
    rf'|(?:结果|输出|返回值|响应|回复)[ \t]*(?:必须|应当|应)[ \t]*(?:为|是|采用)[ \t]*'
    rf'(?:有效的?|纯|严格的?|可解析的?)?[ \t]*[`"\']?{_FORMAT_NAME}(?![A-Za-z0-9_])'
)
_OUTPUT_FORMAT_COMMAND = re.compile(_OUTPUT_COMMAND, re.IGNORECASE)
_OUTPUT_FORMAT_CLAUSE = re.compile(
    rf'(?:{_OUTPUT_COMMAND})[^\r\n]*?(?:[.!?](?=\s|$)|(?=\r?$))', re.IGNORECASE | re.MULTILINE,
)
_OUTPUT_DECLARATION = re.compile(
    r'^[ \t]*+(?:(?:[-*]|\d++[.)])[ \t]++)?(?:'
    rf'(?:\*\*)?{_OUTPUT_TITLE}(?:\*\*)?[ \t]*(?:[:：]|(?=\r?$))'
    rf'|(?:\[{_OUTPUT_TITLE}\]|【{_OUTPUT_TITLE}】)[ \t]*(?:[:：]|(?=\r?$))'
    rf'|(?P<command>{_OUTPUT_COMMAND})'
    r')', re.IGNORECASE | re.MULTILINE,
)
_SCHEMA_DECLARATION = re.compile(
    r'(?im)^[ \t]*(?:`{3,}|~{3,})[ \t]*json[ _-]?schema(?=[ \t\r\n]|$)'
    r'|<(?:response|output|json)[_-]schema\b|"\$schema"[ \t]*:'
    r'|\b(?:response|output)[ \t]+schema\b'
)
_JSON_DELIMITER = re.compile(
    r'(?a:\b)(?:(?:[A-Z0-9]+_){0,4}JSON_(?P<suffix>START|BEGIN|END|STOP)'
    r'|(?P<prefix>START|BEGIN|END|STOP)_(?:[A-Z0-9]+_){0,4}JSON)(?a:\b)'
)
_TEMPLATE_SYNTAX = re.compile(
    r'\{\{[^{}\r\n]*\}\}|\{%[^{}\r\n]*%\}'
    r'|\{[A-Za-z_][\w.\[\]-]*(?:[!:][^{}\r\n]+)?\}'
    r'|%\([A-Za-z_][\w.-]*\)[A-Za-z]|%[A-Za-z_][A-Za-z0-9_]*%'
    r'|</?[A-Za-z][\w.:-]*(?=[\s/>])(?:[^<>"\x27\r\n]|"[^"\r\n]*"|\x27[^\x27\r\n]*\x27)*>'
)
_MEASUREMENT = re.compile(
    r'(?<![A-Za-z0-9_.])[+-]?\d+(?:\.\d+)?[ \t]*'
    r'(?:°[CF]|mm|cm|km|m|µg|μg|mg|g|kg|mL|ml|L|in|ft|oz|lb|mAh|Ah|kWh|Wh|mW|kW|W|mV|V|mA|A|kHz|MHz|GHz|Hz)'
    r'(?:/[A-Za-zµμ]{1,5})?(?![A-Za-z0-9_])'
)
_NUMBER = re.compile(
    r'(?<![A-Za-z0-9_.])[+-]?'
    r'(?:0[xX][0-9A-Fa-f_]+|0[bB][01_]+|0[oO][0-7_]+|'
    r'(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?)'
    r'(?![A-Za-z0-9_]|\.\d)'
)
_TECHNICAL = re.compile(
    r'(?a:\b)[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"\x27`，。！？；：、]*'
    r'|(?<![A-Za-z0-9_:/\\])(?:~?/|\.{1,2}/|[A-Za-z]:[\\/]|\\\\|[A-Za-z0-9_.-]+[\\/])'
    r'[^\s<>"\x27`，。！？；：、]+'
    r'|(?<!\w)--[A-Za-z][\w-]*(?:=[^\s<>"\x27`]+)?'
    r'|\$\{[A-Za-z_]\w*\}|\$[A-Za-z_]\w*'
    r'|(?a:\b(?:[A-Za-z_]\w*_\w+|[A-Z][A-Z0-9_]+|[a-z]+(?:[A-Z][A-Za-z0-9]*)+)\b)'
    r'|\b[\w@+-]+(?:\.[\w@+-]+)+\b'
)
_COLLECTION_TOKEN = re.compile(_QUOTED + r'|[\[\]{}]')
_INDENTED_CODE = re.compile(r'(?m)^(?: {4}|\t)[^\r\n]+')
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


def _declared_literals(tools, response_schemas, stop_sequences, texts):
    schemas = [schema for schema in response_schemas if isinstance(schema, dict)]
    found = {value for value in stop_sequences if isinstance(value, str)}
    if schemas:
        found.update(('null', 'true', 'false'))
    pending = [tools, *schemas]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if isinstance(node.get('name'), str):
                found.add(node['name'])
            properties = node.get('properties')
            if isinstance(properties, dict):
                found.update(key for key in properties if isinstance(key, str))
            for keyword in ('enum', 'required'):
                if isinstance(node.get(keyword), list):
                    found.update(value for value in node[keyword] if isinstance(value, str))
            if isinstance(node.get('const'), str):
                found.add(node['const'])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    found.discard('')
    return sorted(
        (value for value in found if any(value in text for text in texts)),
        key=lambda value: (-len(value), value),
    )


def _fenced_spans(text):
    # Preserve even an unfinished fenced block rather than corrupt partial code.
    pos = 0
    while match := _FENCES.search(text, pos):
        fence = match['fence']
        closing = re.compile(r'(?m)^[ \t]*' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*(?:\r?\n|$)')
        close = closing.search(text, match.end())
        end = close.end() if close else len(text)
        yield match.start(), end
        pos = end


def _matches_outside_blocks(pattern, text, blocks):
    pos = 0
    for start, end in blocks:
        if start > pos:
            yield from pattern.finditer(text, pos, start)
        pos = max(pos, end)
    yield from pattern.finditer(text, pos)


def _technical_spans(text, profile, literal_blocks):
    blocks = list(_fenced_spans(text))
    blocks.extend(match.span() for match in _TECHNICAL_BLOCK.finditer(text))
    for start, end in blocks:
        yield start, end, False

    opaque = sorted([*blocks, *literal_blocks])
    headings = list(_matches_outside_blocks(_HEADINGS, text, opaque))
    labels = _PROFILE_HEADINGS['generic'] + _PROFILE_HEADINGS.get(profile, ())
    for index, heading in enumerate(headings):
        label = heading[2].strip().rstrip(':：').strip('* ').lower()
        is_output = _OUTPUT_HEADING.fullmatch(label) is not None
        if not is_output and label not in labels:
            continue
        end = next((other.start() for other in headings[index + 1:] if len(other[1]) <= len(heading[1])), len(text))
        yield heading.start(), end, is_output

    # A standalone format directive can define unmarked enum values or examples on the following lines.
    heading_starts = [heading.start() for heading in headings] + [len(text)]
    for match in _matches_outside_blocks(_OUTPUT_DECLARATION, text, opaque):
        start = match.start()
        end = heading_starts[bisect_right(heading_starts, match.start())]
        if match['command']:
            if blank_line := _PARAGRAPH_BREAK.search(text, match.end(), end):
                end = blank_line.start()
            # The command itself is covered by _OUTPUT_FORMAT_CLAUSE.
            start = text.find('\n', match.end(), end)
            if start == -1:
                continue
        yield start, end, True

    for pattern in (_TEMPLATE_SYNTAX, _MEASUREMENT, _NUMBER, _INDENTED_CODE):
        for match in pattern.finditer(text):
            yield match.start(), match.end(), False
    for match in _TECHNICAL.finditer(text):
        start, end = match.span()
        # Sentence punctuation is prose, not part of a bare path or URL.
        end = start + len(text[start:end].rstrip('.,;:!?)]}'))
        if end > start:
            yield start, end, False


def declares_structured_output(text: str) -> bool:
    """Recognize a caller-owned output contract without deriving one from an example."""
    if (_OUTPUT_FORMAT_COMMAND.search(text) or _OUTPUT_DECLARATION.search(text)
            or _SCHEMA_DECLARATION.search(text)):
        return True
    if any(_OUTPUT_HEADING.fullmatch(heading[2].strip(' \t*:：')) for heading in _HEADINGS.finditer(text)):
        return True
    markers = {match['prefix'] or match['suffix'] for match in _JSON_DELIMITER.finditer(text)}
    return bool(markers & {'START', 'BEGIN'} and markers & {'END', 'STOP'})


def _collection_spans(text):
    """Preserve bracketed data, including Python literals and JSON examples."""
    openings = []
    matching_open = {']': '[', '}': '{'}
    for token in _COLLECTION_TOKEN.finditer(text):
        character = token[0]
        if character in ('[', '{'):
            openings.append((character, token.start()))
        elif character in matching_open:
            if openings and openings[-1][0] == matching_open[character]:
                _, start = openings.pop()
                yield start, token.end(), False
            else:
                openings.clear()


def _protected_spans(text, profile, declared, protect_technical):
    # These spans have to remain exact in both modes.
    literal_blocks = [match.span() for match in _LITERAL_BLOCK.finditer(text)]
    spans = [(start, end, False) for start, end in literal_blocks]
    spans.extend((*match.span(), True) for pattern in (_OUTPUT_CONTRACT, _OUTPUT_FORMAT_CLAUSE)
                 for match in pattern.finditer(text))
    spans.extend((*match.span(), False) for match in _VERBATIM.finditer(text))
    spans.extend(_collection_spans(text))
    if protect_technical:
        spans.extend(_technical_spans(text, profile, literal_blocks))
    if declared:
        spans.extend((*match.span(), False) for match in declared.finditer(text))
    merged = []
    for start, end, keep_plain in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end, previous_plain = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end), previous_plain or keep_plain)
        else:
            merged.append((start, end, keep_plain))
    return merged


def protected_system_parts(
    parts, tools, profile='generic', *, response_schemas=(), stop_sequences=(),
    protect_technical=True, structured_output=False,
):
    texts = [p['text'] for p in parts if isinstance(p, dict) and isinstance(p.get('text'), str)]
    if not any(text.strip() for text in texts):
        return list(parts)
    profile = profile if profile in _PROFILE_HEADINGS else 'generic'
    plain_literals = protect_technical or structured_output
    tag = 'gcli_literal'
    while not plain_literals and any(tag in text for text in texts):
        tag += '_1'
    literals = _declared_literals(tools, response_schemas, stop_sequences, texts)
    declared = None
    if literals:
        declared = re.compile(r'(?<!\w)(?:' + '|'.join(map(re.escape, literals)) + r')(?!\w)', re.ASCII)
    result = []
    has_literals = False
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get('text'), str):
            result.append(part)
            continue
        text = part['text']
        chunks = []
        cursor = 0
        for start, end, keep_plain in _protected_spans(text, profile, declared, plain_literals):
            chunks.append(text[cursor:start].translate(VOWEL_TRANSLATION))
            literal = text[start:end]
            if plain_literals or keep_plain:
                chunks.append(literal)
            else:
                has_literals = True
                chunks.extend((f'<{tag}>', literal, f'</{tag}>'))
            cursor = end
        chunks.append(text[cursor:].translate(VOWEL_TRANSLATION))
        result.append({**part, 'text': ''.join(chunks)})
    if result == list(parts):
        return result
    literal_guidance = (
        f'Content inside <{tag}>...</{tag}> is verbatim original text: never decode, normalize or rewrite it. '
        'The boundary tags are input adapter markup, never add them to your answer. '
    ) if has_literals else ''
    return [{'text': DECODER.format(profile=profile, literal_guidance=literal_guidance)}, *result]
