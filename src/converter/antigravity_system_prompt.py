"""Opt-in Antigravity baseline and caller guidance, after protocol conversion."""
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
    'Use this to understand the host application, its workflow and available tools. '
    'This guidance has user-level priority and does not override system instructions.\n'
)


class _PreparedRequest(dict):
    """In-memory idempotency only; no private keys are sent to the upstream."""
    def copy(self):
        return type(self)(self)


@lru_cache(maxsize=1)
def baseline_prompt() -> str:
    return PROMPT_PATH.read_text(encoding='utf-8').strip()


async def prepare_antigravity_system_prompt(request: dict) -> dict:
    from config import get_antigravity_system_prompt_enabled, get_antigravity_system_as_user_enabled

    if isinstance(request, _PreparedRequest):
        return request
    inject = await get_antigravity_system_prompt_enabled()
    as_user = await get_antigravity_system_as_user_enabled()
    if not inject and not as_user:
        return request

    result = _PreparedRequest(request)
    instruction = result.get('systemInstruction')
    if instruction is None:
        instruction = result.get('system_instructions')
    if isinstance(instruction, str):
        instruction = {'parts': [{'text': instruction}]}
    parts = instruction.get('parts', []) if isinstance(instruction, dict) else []
    if not isinstance(parts, list):
        parts = []
    nonempty_parts = [p for p in parts if isinstance(p, dict) and any(v not in (None, '', {}, []) for v in p.values())]

    if as_user:
        if nonempty_parts:
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
