"""Antigravity baseline and caller guidance, after protocol conversion."""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from src.converter.protected_leet import (
    declares_structured_output,
    detect_client_profile,
    instruction_text,
    protected_system_parts,
)

DEFAULT_PROMPT_VERSION = '2.35.2'
# 兼容既有引用；真正的版本由 baseline_prompt() 按运行期配置/账号解析。
PROMPT_VERSION = DEFAULT_PROMPT_VERSION
PROMPT_SCOPE = '官方 2.35.2 完整 system（去掉运行时 <user_information> 块）；单文件整段加载，不做分段拼接'
PROMPT_DIR = Path(__file__).resolve().parent.parent / 'prompts'
PROMPT_PATH = PROMPT_DIR / f'antigravity-{DEFAULT_PROMPT_VERSION}.txt'

# 基线在选号之前不可知（要跟随账号的 hub 版本）。注入时先用部署级默认版本，
# 选号之后由 resolve_pending_baseline() 在 wrap_cli_request 里按账号版本做一次
# 前缀替换；同版本时两个前缀字符串相等，直接返回，零额外开销。

BRIDGE = (
    '=== SESSION MODE: CALLER-MANAGED ===\n'
    'This session runs in caller-managed mode. The host-application document above is retained for '
    'compatibility and describes a different product; in this mode it is background reference only. '
    "The caller's system instructions define the operative role, language, scope, output format and tool "
    'permissions for this session. Host-application personas, tools, artifact locations and sandbox '
    'settings do not apply. Answer in text only unless a tool is declared in this request.'
)
BRIDGE_REQUIREMENTS = (
    ('session_mode_marker', '=== SESSION MODE: CALLER-MANAGED ==='),
    ('host_retained_for_compat', 'retained for compatibility'),
    ('host_is_background', 'background reference only'),
    ('caller_defines_contract', "caller's system instructions define the operative"),
    ('host_settings_inactive', 'do not apply'),
    ('text_only_default', 'Answer in text only unless a tool is declared'),
)


def validate_bridge(text: str | None = None) -> list:
    """校验引导词是否仍覆盖“官方块在前、客户端 system 说了算”的全部约束。"""
    body = BRIDGE if text is None else text
    return [key for key, needle in BRIDGE_REQUIREMENTS if needle not in body]


GUIDANCE = (
    'Client-provided guidance (original system instructions, presented at user role):\n'
    'Read and understand the following client-provided prompt before responding to subsequent user tasks. '
    'Apply its relevant guidance when carrying out those tasks. '
    'Do not send a separate acknowledgment or treat this prompt as the user task. '
    'Use this to understand the host application, its workflow and available tools. '
    'This guidance has user-level priority and does not override system instructions.\n'
)
STRUCTURED_OUTPUT_PRIORITY = (
    'Structured response priority. For this task, the effective response schema and the applicable output '
    'contract take precedence over the earlier generic coding-assistant presentation rules. Follow the exact '
    'field names, literal values and required delimiters. Omit code or file links, Markdown formatting, '
    'extra commentary and follow-up questions unless the output contract explicitly requires them. '
    'When JSON alone is requested, return a single bare JSON value without a Markdown code fence.'
)


class _PreparedRequest(dict):
    """In-memory idempotency only; no private keys are sent to the upstream."""
    def copy(self):
        result = type(self)(self)
        result.__dict__.update(self.__dict__)
        return result


def _configured_prompt_version() -> str:
    """部署级默认基线版本：跟随 hub 版本配置，非法值回退到内置版本。"""
    try:
        from config import get_antigravity_hub_version_sync

        version = get_antigravity_hub_version_sync()
    except Exception:
        return DEFAULT_PROMPT_VERSION
    if isinstance(version, str) and version.count('.') == 2 and version.replace('.', '').isdigit():
        return version
    return DEFAULT_PROMPT_VERSION


@lru_cache(maxsize=8)
def _baseline_for_version(version: str) -> str:
    path = PROMPT_DIR / f'antigravity-{version}.txt'
    if not path.is_file():
        path = PROMPT_DIR / f'antigravity-{DEFAULT_PROMPT_VERSION}.txt'
    return path.read_text(encoding='utf-8').strip()


def baseline_prompt(version: str | None = None) -> str:
    """取基线提示词；version 为空时用部署配置的 hub 版本。

    只支持已经随仓库分发基线的版本，缺失时回退到内置版本——宁可版本不符，
    也不要在线上生成一个内容不可控的 system。
    """
    return _baseline_for_version(version or _configured_prompt_version())


def _host_prefix(version: str | None = None) -> str:
    return baseline_prompt(version) + '\n\n' + BRIDGE


def resolve_pending_baseline(request: dict, version: str | None) -> dict:
    """按账号版本替换已注入的基线前缀；版本相同或没有注入时原样返回。

    在选号之后调用（wrap_cli_request），因此 system 基线与账号 UA 声明的
    版本一致。缺失该版本基线文件时 baseline_prompt 会回退到内置版本，
    于是 old == new，函数直接返回，不会产生半套内容。
    """
    if not version:
        return request
    old = _host_prefix()
    new = _host_prefix(version)
    if old == new:
        return request
    instruction = request.get('systemInstruction')
    if not isinstance(instruction, dict):
        return request
    parts = instruction.get('parts')
    if not isinstance(parts, list) or not parts:
        return request
    head = parts[0]
    if not isinstance(head, dict) or not isinstance(head.get('text'), str):
        return request
    text = head['text']
    if not text.startswith(old):
        return request
    return {
        **request,
        'systemInstruction': {
            **instruction,
            'parts': [{'text': new + text[len(old):]}, *parts[1:]],
        },
    }


def _caller_controls_output(generation: dict, system_text: str, contents) -> bool:
    mime_type = generation.get('responseMimeType')
    if isinstance(mime_type, str) and mime_type.partition(';')[0].strip().lower() == 'application/json':
        return True
    if any(generation.get(key) is not None for key in ('responseSchema', 'responseJsonSchema')):
        return True
    if declares_structured_output(system_text):
        return True
    if isinstance(contents, list):
        for turn in reversed(contents):
            if isinstance(turn, dict) and turn.get('role', 'user') == 'user':
                text = instruction_text(turn)
                if text.strip():
                    return declares_structured_output(text)
    return False


async def prepare_antigravity_system_prompt(
    request: dict, *, client_profile: str | None = None, api_key_id: str | None = None,
) -> dict:
    from config import (
        get_antigravity_system_prompt_enabled,
        get_antigravity_system_as_user_enabled,
        get_antigravity_system_obfuscation_mode,
    )

    if isinstance(request, _PreparedRequest):
        return request
    from src.workflow_contracts import get_workflow_contract

    workflow = get_workflow_contract(api_key_id)
    if workflow is not None:
        from src.api.antigravity import is_antigravity_image_request
        if is_antigravity_image_request(request.get('model', ''), request):
            workflow = None
    inject = await get_antigravity_system_prompt_enabled()
    as_user = await get_antigravity_system_as_user_enabled()
    obfuscation_mode = await get_antigravity_system_obfuscation_mode()
    result = _PreparedRequest(request)
    result.workflow_contract = workflow
    result.system_obfuscation_mode = obfuscation_mode
    if workflow is not None:
        workflow.apply_generation_config(result)
    original_instruction = request.get("systemInstruction")
    if original_instruction is None:
        original_instruction = request.get("system_instructions")
    result.fusion_instruction = deepcopy(original_instruction)
    result.fusion_moved_guidance = False
    generation = result.get('generationConfig') or {}
    system_text = instruction_text(original_instruction)
    result.preserve_output_format = workflow is None and _caller_controls_output(
        generation, system_text, result.get('contents'),
    )
    structured = workflow is not None or result.preserve_output_format
    obfuscate = obfuscation_mode != 'off'
    if not inject and not as_user and not obfuscate and not structured:
        return result
    instruction = original_instruction
    if isinstance(instruction, str):
        instruction = {'parts': [{'text': instruction}]}
    parts = instruction.get('parts', []) if isinstance(instruction, dict) else []
    if not isinstance(parts, list):
        parts = []
    caller_parts = [p for p in parts if isinstance(p, dict) and any(
        v.strip() if isinstance(v, str) else v not in (None, {}, []) for v in p.values()
    )]
    host_part = None
    if caller_parts:
        first_text = caller_parts[0].get('text')
        # With injection enabled, reuse only the full prefix this gateway emits.
        expected_prefix = _host_prefix() if inject else baseline_prompt().partition('\n')[0]
        if isinstance(first_text, str) and first_text.startswith(expected_prefix):
            host_part, *caller_parts = caller_parts
    if inject and host_part is None:
        host_part = {'text': _host_prefix()}

    if obfuscate:
        profile = client_profile or detect_client_profile(system_text)
        schemas = [generation.get('responseSchema'), generation.get('responseJsonSchema')]
        stops = generation.get('stopSequences')
        stop_sequences = list(stops) if isinstance(stops, list) else []
        if workflow is not None:
            schemas.append(workflow.response_schema)
            stop_sequences.extend(workflow.legacy_markers or ())
        caller_parts = protected_system_parts(
            caller_parts, result.get('tools', []), profile,
            response_schemas=schemas, stop_sequences=stop_sequences,
            protect_technical=obfuscation_mode == 'protected',
            structured_output=structured,
        )

    if as_user:
        if caller_parts:
            result.fusion_moved_guidance = True
            result['contents'] = [
                {'role': 'user', 'parts': [{'text': GUIDANCE}, *caller_parts]},
                *(result.get('contents') or []),
            ]
        result.pop('systemInstruction', None)
        result.pop('system_instructions', None)

    if host_part is not None or workflow is not None or (not as_user and obfuscate and caller_parts != parts):
        system_parts = []
        if host_part is not None:
            system_parts.append(host_part)
            if structured:
                system_parts.append({'text': STRUCTURED_OUTPUT_PRIORITY})
        if not as_user:
            system_parts.extend(caller_parts)
        if workflow is not None:
            system_parts.append({'text': workflow.json_guidance})
        system = instruction if not inject and not as_user and isinstance(instruction, dict) else {}
        result['systemInstruction'] = {**system, 'parts': system_parts}
        result.pop('system_instructions', None)
    return result
