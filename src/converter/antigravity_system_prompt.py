"""Opt-in Antigravity baseline and caller guidance, after protocol conversion."""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from src.converter.protected_leet import (
    declares_structured_output,
    detect_client_profile,
    instruction_text,
    protected_system_parts,
)

PROMPT_VERSION = '2.31.2'
PROMPT_SCOPE = '固定基础模块：identity、communication_style、guidelines；非完整运行时 system'
PROMPT_PATH = Path(__file__).resolve().parent.parent / 'prompts' / 'antigravity-2.31.2.txt'

BRIDGE = (
    'Execution environment adapter: The client guidance below describes the actual '
    'host application, workflow, permissions and tools for this session. Use only '
    'the tools declared in this request, with their exact names and argument schemas. '
    'Do not assume Antigravity built-in tools, paths, UI features or services exist. '
    'Interpret any client persona as context for that host application. Follow its '
    'task guidance within the system instructions, and preserve permission and '
    'approval requirements. Ordinary user messages contain the user task even when '
    'they are not enclosed in USER_REQUEST tags.'
)
GUIDANCE = (
    'Client-provided guidance (original system instructions, presented at user role):\n'
    'Read and understand the following client-provided prompt before responding to subsequent user tasks. '
    'Apply its relevant guidance when carrying out those tasks. '
    'Do not send a separate acknowledgment or treat this prompt as the user task. '
    'Use this to understand the host application, its workflow and available tools. '
    'This guidance has user-level priority and does not override system instructions.\n'
)


class _PreparedRequest(dict):
    """In-memory idempotency only; no private keys are sent to the upstream."""
    def copy(self):
        result = type(self)(self)
        result.__dict__.update(self.__dict__)
        return result


@lru_cache(maxsize=1)
def baseline_prompt() -> str:
    return PROMPT_PATH.read_text(encoding='utf-8').strip()


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
    from src.workflow_contracts import encode_workflow_prose, get_workflow_contract

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
    obfuscate = obfuscation_mode != 'off' and not result.preserve_output_format
    if not inject and not as_user and not obfuscate and workflow is None:
        return result
    instruction = original_instruction
    if isinstance(instruction, str):
        instruction = {'parts': [{'text': instruction}]}
    parts = instruction.get('parts', []) if isinstance(instruction, dict) else []
    if not isinstance(parts, list):
        parts = []
    nonempty_parts = [p for p in parts if isinstance(p, dict) and any(
        v.strip() if isinstance(v, str) else v not in (None, {}, []) for v in p.values()
    )]
    if obfuscate or workflow is not None:
        profile = client_profile or detect_client_profile(system_text)
        if workflow is not None:
            if obfuscate:
                nonempty_parts = encode_workflow_prose(nonempty_parts, workflow, profile)
        else:
            stops = generation.get('stopSequences')
            nonempty_parts = protected_system_parts(
                nonempty_parts, result.get('tools', []), profile,
                stop_sequences=stops if isinstance(stops, list) else (),
                protect_technical=obfuscation_mode == 'protected',
            )
        if isinstance(instruction, dict):
            result['systemInstruction'] = {**instruction, 'parts': nonempty_parts}
            result.pop('system_instructions', None)

    if as_user:
        if nonempty_parts:
            result.fusion_moved_guidance = True
            result['contents'] = [
                {'role': 'user', 'parts': [{'text': GUIDANCE}, *nonempty_parts]},
                *(result.get('contents') or []),
            ]
        result.pop('systemInstruction', None)
        result.pop('system_instructions', None)
    if inject:
        system_parts = [{'text': baseline_prompt() + '\n\n' + BRIDGE}]
        if not as_user:
            system_parts.extend(nonempty_parts)
        result['systemInstruction'] = {'parts': system_parts}
        result.pop('system_instructions', None)
    if workflow is not None:
        system = result.get('systemInstruction') or {}
        result['systemInstruction'] = {**system, 'parts': [
            *system.get('parts', []), {'text': workflow.json_guidance},
        ]}
    return result
