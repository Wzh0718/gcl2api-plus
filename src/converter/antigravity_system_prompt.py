"""Opt-in Antigravity baseline and caller guidance, after protocol conversion."""
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

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
VOWEL_TRANSLATION = str.maketrans('aeiouAEIOU', '4310v4310V')


class _PreparedRequest(dict):
    """In-memory idempotency only; no private keys are sent to the upstream."""
    def copy(self):
        result = type(self)(self)
        result.__dict__.update(self.__dict__)
        return result


@lru_cache(maxsize=1)
def baseline_prompt() -> str:
    return PROMPT_PATH.read_text(encoding='utf-8').strip()


async def prepare_antigravity_system_prompt(request: dict, *, client_profile: str | None = None) -> dict:
    from config import (
        get_antigravity_system_prompt_enabled,
        get_antigravity_system_as_user_enabled,
        get_antigravity_system_obfuscation_mode,
    )

    if isinstance(request, _PreparedRequest):
        return request
    inject = await get_antigravity_system_prompt_enabled()
    as_user = await get_antigravity_system_as_user_enabled()
    obfuscation_mode = await get_antigravity_system_obfuscation_mode()
    obfuscate = obfuscation_mode != 'off'
    result = _PreparedRequest(request)
    original_instruction = request.get("systemInstruction")
    if original_instruction is None:
        original_instruction = request.get("system_instructions")
    result.fusion_instruction = deepcopy(original_instruction)
    result.fusion_moved_guidance = False
    if not inject and not as_user and not obfuscate:
        return result
    instruction = result.get('systemInstruction')
    if instruction is None:
        instruction = result.get('system_instructions')
    if isinstance(instruction, str):
        instruction = {'parts': [{'text': instruction}]}
    parts = instruction.get('parts', []) if isinstance(instruction, dict) else []
    if not isinstance(parts, list):
        parts = []
    nonempty_parts = [p for p in parts if isinstance(p, dict) and any(
        v.strip() if isinstance(v, str) else v not in (None, {}, []) for v in p.values()
    )]
    if obfuscate:
        if obfuscation_mode == 'protected':
            from src.converter.protected_leet import protected_system_parts, detect_client_profile, instruction_text
            profile = client_profile or detect_client_profile(instruction_text(instruction))
            nonempty_parts = protected_system_parts(nonempty_parts, result.get('tools', []), profile)
        else:
            nonempty_parts = [
                {**p, 'text': p['text'].translate(VOWEL_TRANSLATION)}
                if isinstance(p.get('text'), str) else p for p in nonempty_parts
            ]
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
    return result
